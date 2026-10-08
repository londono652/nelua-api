"""DynamoStore contra una DynamoDB simulada (moto)."""

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from app.models import Deploy
from app.store import DynamoStore

REPO = "londono652/nelua-api"


def deploy(id_, days_ago, environment="staging") -> Deploy:
    return Deploy(
        id=id_,
        environment=environment,
        status="success",
        github_state="success",
        sha="abc",
        ref="main",
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


@pytest.fixture
def dynamo(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        store = DynamoStore("nelua-api-test", "us-east-2", session=boto3.session.Session())
        store.create_table_if_missing()
        store.create_table_if_missing()  # la segunda vez no hace nada
        yield store


async def test_deploy_history_by_environment_and_window(dynamo):
    first = deploy(1, 1)
    await dynamo.put_deploys(
        REPO, [first, deploy(2, 40), deploy(3, 2, "prod"), deploy(4, 3, "preview")]
    )
    since = datetime.now(UTC) - timedelta(days=31)
    recent = await dynamo.recent_deploys(REPO, ("staging", "prod"), since)
    assert sorted(d.id for d in recent) == [1, 3]

    # Escribir de nuevo el mismo despliegue lo reemplaza, no lo duplica.
    await dynamo.put_deploys(REPO, [first.model_copy(update={"status": "failure"})])
    recent = await dynamo.recent_deploys(REPO, ("staging",), since)
    assert [(d.id, d.status) for d in recent] == [(1, "failure")]


async def test_history_items_expire(dynamo):
    await dynamo.put_deploys(REPO, [deploy(1, 1)])
    item = dynamo._table.scan()["Items"][0]
    assert item["expires_at"] > datetime.now(UTC).timestamp() + 80 * 24 * 3600


async def test_snapshots_roundtrip(dynamo):
    await dynamo.put_snapshot("cluster", [{"name": "x"}], "sample", 15)
    snapshots = await dynamo.get_snapshots(["cluster", "deploys#otro/repo"])
    assert list(snapshots) == ["cluster"]  # la que no existe simplemente no viene
    snapshot = snapshots["cluster"]
    assert snapshot.payload == [{"name": "x"}]
    assert (snapshot.source, snapshot.refresh_seconds) == ("sample", 15)
    assert snapshot.collected_at <= datetime.now(UTC)
    assert await dynamo.get_snapshots([]) == {}
