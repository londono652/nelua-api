"""Almacén compartido entre el recolector y los pods de la API (DynamoDB).

Una sola tabla con dos tipos de registro:

  pk = "deploy#<owner>/<repo>#<ambiente>"   sk = "<fecha>#<id>"
      Historial de despliegues. Se guarda para calcular métricas en una
      ventana de días y para no volver a preguntarle a GitHub por despliegues
      que ya terminaron. Expira solo a los 90 días (TTL de DynamoDB).

  pk = "snapshot"                           sk = <nombre>
      La última foto de cada vista, ya calculada por el recolector. Es lo único
      que leen los pods de la API.

El recolector escribe; los pods de la API solo leen, unas pocas veces por
segundo en total, sin importar cuántas peticiones reciban.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
from boto3.dynamodb.conditions import Key

from app.models import Deploy

HISTORY_TTL_SECONDS = 90 * 24 * 3600


class Snapshot(dict):
    """Una foto guardada: los datos, cuándo se tomaron y cada cuánto se renuevan."""

    @property
    def payload(self) -> Any:
        return self["payload"]

    @property
    def collected_at(self) -> datetime:
        return datetime.fromisoformat(self["collected_at"])

    @property
    def refresh_seconds(self) -> int:
        return int(self["refresh_seconds"])

    @property
    def source(self) -> str:
        return self["source"]


class Store(Protocol):
    async def put_deploys(self, repo: str, deploys: list[Deploy]) -> None: ...

    async def recent_deploys(
        self, repo: str, environments: tuple[str, ...], since: datetime
    ) -> list[Deploy]: ...

    async def put_snapshot(
        self, name: str, payload: Any, source: str, refresh_seconds: int
    ) -> None: ...

    async def get_snapshots(self, names: list[str]) -> dict[str, Snapshot]: ...


def deploy_pk(repo: str, environment: str) -> str:
    return f"deploy#{repo}#{environment}"


def deploy_sk(deploy: Deploy) -> str:
    return f"{deploy.created_at.isoformat()}#{deploy.id}"


def _snapshot(payload: Any, source: str, refresh_seconds: int) -> dict[str, Any]:
    return {
        "payload": payload,
        "source": source,
        "refresh_seconds": refresh_seconds,
        "collected_at": datetime.now(UTC).isoformat(),
    }


class DynamoStore:
    def __init__(
        self,
        table_name: str,
        region: str,
        endpoint_url: str = "",
        session: Any = None,
    ) -> None:
        session = session or boto3.session.Session()
        resource = session.resource(
            "dynamodb", region_name=region, endpoint_url=endpoint_url or None
        )
        self._client = resource.meta.client
        self._table = resource.Table(table_name)
        self._table_name = table_name

    def create_table_if_missing(self) -> None:
        """Solo para desarrollo local (DynamoDB Local). En AWS la crea Terraform."""
        existing = self._client.list_tables()["TableNames"]
        if self._table_name in existing:
            return
        self._client.create_table(
            TableName=self._table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        self._client.get_waiter("table_exists").wait(TableName=self._table_name)

    # ---------- Historial de despliegues ----------

    def _put_deploys(self, repo: str, deploys: list[Deploy]) -> None:
        expires = int(time.time()) + HISTORY_TTL_SECONDS
        with self._table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for deploy in deploys:
                batch.put_item(
                    Item={
                        "pk": deploy_pk(repo, deploy.environment),
                        "sk": deploy_sk(deploy),
                        "data": deploy.model_dump_json(),
                        "expires_at": expires,
                    }
                )

    async def put_deploys(self, repo: str, deploys: list[Deploy]) -> None:
        await asyncio.to_thread(self._put_deploys, repo, deploys)

    def _recent_deploys(
        self, repo: str, environments: tuple[str, ...], since: datetime
    ) -> list[Deploy]:
        deploys: list[Deploy] = []
        for environment in environments:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(deploy_pk(repo, environment))
                & Key("sk").gte(since.isoformat()),
            }
            while True:
                page = self._table.query(**kwargs)
                deploys += [Deploy.model_validate_json(item["data"]) for item in page["Items"]]
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return deploys

    async def recent_deploys(
        self, repo: str, environments: tuple[str, ...], since: datetime
    ) -> list[Deploy]:
        return await asyncio.to_thread(self._recent_deploys, repo, environments, since)

    # ---------- Fotos ----------

    def _put_snapshot(self, name: str, payload: Any, source: str, refresh_seconds: int) -> None:
        self._table.put_item(
            Item={
                "pk": "snapshot",
                "sk": name,
                "data": json.dumps(_snapshot(payload, source, refresh_seconds)),
            }
        )

    async def put_snapshot(
        self, name: str, payload: Any, source: str, refresh_seconds: int
    ) -> None:
        await asyncio.to_thread(self._put_snapshot, name, payload, source, refresh_seconds)

    def _get_snapshots(self, names: list[str]) -> dict[str, Snapshot]:
        if not names:
            return {}
        response = self._client.batch_get_item(
            RequestItems={
                self._table_name: {
                    "Keys": [{"pk": "snapshot", "sk": name} for name in names],
                    "ProjectionExpression": "sk, #d",
                    "ExpressionAttributeNames": {"#d": "data"},
                }
            }
        )
        items = response["Responses"].get(self._table_name, [])
        return {item["sk"]: Snapshot(json.loads(item["data"])) for item in items}

    async def get_snapshots(self, names: list[str]) -> dict[str, Snapshot]:
        return await asyncio.to_thread(self._get_snapshots, names)


class MemoryStore:
    """Misma interfaz que DynamoStore, en memoria. Se usa en las pruebas."""

    def __init__(self) -> None:
        self.deploys: dict[str, dict[str, Deploy]] = {}
        self.snapshots: dict[str, Snapshot] = {}

    async def put_deploys(self, repo: str, deploys: list[Deploy]) -> None:
        for deploy in deploys:
            self.deploys.setdefault(deploy_pk(repo, deploy.environment), {})[deploy_sk(deploy)] = (
                deploy
            )

    async def recent_deploys(
        self, repo: str, environments: tuple[str, ...], since: datetime
    ) -> list[Deploy]:
        result = []
        for environment in environments:
            for sk, deploy in self.deploys.get(deploy_pk(repo, environment), {}).items():
                if sk >= since.isoformat():
                    result.append(deploy)
        return result

    async def put_snapshot(
        self, name: str, payload: Any, source: str, refresh_seconds: int
    ) -> None:
        # Pasa por JSON igual que DynamoStore, para que las pruebas vean lo mismo.
        self.snapshots[name] = Snapshot(
            json.loads(json.dumps(_snapshot(payload, source, refresh_seconds)))
        )

    async def get_snapshots(self, names: list[str]) -> dict[str, Snapshot]:
        return {name: self.snapshots[name] for name in names if name in self.snapshots}
