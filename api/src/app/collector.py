"""Recolector: el único proceso que habla con las fuentes externas.

Corre con UNA réplica, aparte de la API. Consulta Kubernetes y GitHub cada
cierto tiempo, guarda el historial y escribe en DynamoDB una foto ya calculada
de cada vista. Los pods de la API solo leen esas fotos.

Así la carga sobre las fuentes es la misma con 10 o con 10.000 peticiones por
segundo, y GitHub (que limita las peticiones por hora) recibe un solo cliente.

    python -m app.collector
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import Settings, load_settings
from app.deploys import WINDOWS, all_stats, latest
from app.models import ClusterState, Deploy
from app.sources.base import BudgetSource, ClusterSource, DeploySource
from app.store import DynamoStore, Store

logger = logging.getLogger("nelua.collector")


def cluster_views(state: ClusterState) -> list[dict]:
    """Une cada deployment con su historial de revisiones (la más reciente primero)."""
    history: dict[tuple[str, str], list[dict]] = {}
    for revision in sorted(state.revisions, key=lambda r: r.revision, reverse=True):
        history.setdefault((revision.namespace, revision.deployment), []).append(
            revision.model_dump(mode="json")
        )
    return [
        {**d.model_dump(mode="json"), "history": history.get((d.namespace, d.name), [])}
        for d in state.deployments
    ]


# Foto con el estado de la última sincronización de cada vista.
SYNC_SNAPSHOT = "sync"
BUDGET_SNAPSHOT = "budget"


def deploys_snapshot_name(repo: str) -> str:
    return f"deploys#{repo}"


class SyncMetrics(Protocol):
    async def record(self, source: str, ok: bool) -> None: ...


class Collector:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        cluster_source: ClusterSource,
        deploy_source: DeploySource,
        metrics: SyncMetrics | None = None,
        budget_source: BudgetSource | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cluster = cluster_source
        self._deploys = deploy_source
        self._metrics = metrics
        self._budgets = budget_source
        self.sync_status: dict[str, dict[str, Any]] = {}

    async def collect_cluster(self) -> None:
        state = await self._cluster.collect()
        await self._store.put_snapshot(
            "cluster",
            cluster_views(state),
            self._cluster.name,
            self._settings.cluster_refresh_seconds,
        )

    async def collect_repo(self, repo: str) -> None:
        environments = self._settings.deploy_environments
        # Se trae un día más que la ventana más larga, para no cortar despliegues en el borde.
        since = datetime.now(UTC) - timedelta(days=max(WINDOWS) + 1)
        history = await self._store.recent_deploys(repo, environments, since)
        known = {deploy.id: deploy for deploy in history}

        fresh = await self._deploys.list_deploys(repo, known)
        changed = [deploy for deploy in fresh if known.get(deploy.id) != deploy]
        if changed:
            await self._store.put_deploys(repo, changed)

        merged: list[Deploy] = list({**known, **{d.id: d for d in fresh}}.values())
        payload = {
            "latest": [d.model_dump(mode="json") for d in latest(merged)],
            "stats": [s.model_dump(mode="json") for s in all_stats(merged, environments)],
        }
        await self._store.put_snapshot(
            deploys_snapshot_name(repo),
            payload,
            self._deploys.name,
            self._settings.github_refresh_seconds,
        )
        logger.info("%s: %d despliegues, %d nuevos o actualizados", repo, len(merged), len(changed))

    async def collect_budget(self) -> None:
        if self._budgets is None:
            return
        budgets = await self._budgets.collect()
        await self._store.put_snapshot(
            BUDGET_SNAPSHOT,
            [b.model_dump(mode="json") for b in budgets],
            self._budgets.name,
            self._settings.budget_refresh_seconds,
        )

    async def collect_deploys(self) -> None:
        for repo in self._settings.github_repos:
            await self.collect_repo(repo)

    # ---------- Estado de la sincronización ----------

    async def _attempt(self, view: str, collect: Callable[[], Awaitable[None]]) -> bool:
        """Ejecuta una recolección y anota cómo le fue, sin dejar escapar el error."""
        attempted_at = datetime.now(UTC).isoformat()
        error = None
        try:
            await collect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # La foto anterior sigue en DynamoDB; la API la sigue sirviendo,
            # marcada como desactualizada, y dice por qué en meta.sync.
            logger.exception("Falló la recolección de %s", view)
            error = describe_error(exc)
        previous = self.sync_status.get(view, {})
        self.sync_status[view] = {
            "last_attempt_at": attempted_at,
            "last_success_at": attempted_at if error is None else previous.get("last_success_at"),
            "consecutive_failures": 0
            if error is None
            else previous.get("consecutive_failures", 0) + 1,
            "error": error,
        }
        return error is None

    async def _report(self, source: str, ok: bool) -> None:
        """Guarda el estado para la API y avisa a CloudWatch (de ahí salen las alarmas)."""
        try:
            await self._store.put_snapshot(
                SYNC_SNAPSHOT, self.sync_status, "collector", self._settings.cluster_refresh_seconds
            )
        except Exception:
            logger.exception("No se pudo guardar el estado de la sincronización")
        if self._metrics is not None:
            try:
                await self._metrics.record(source, ok)
            except Exception:
                logger.exception("No se pudo publicar la métrica de sincronización")

    async def sync_cluster(self) -> None:
        ok = await self._attempt("cluster", self.collect_cluster)
        await self._report("cluster", ok)

    async def sync_deploys(self) -> None:
        # Cada repo por separado: si uno falla (por ejemplo, lo renombraron),
        # los demás se siguen actualizando.
        results = [
            await self._attempt(deploys_snapshot_name(repo), partial(self.collect_repo, repo))
            for repo in self._settings.github_repos
        ]
        await self._report("github", all(results))

    async def sync_budget(self) -> None:
        ok = await self._attempt(BUDGET_SNAPSHOT, self.collect_budget)
        await self._report("budget", ok)

    def _beat(self) -> None:
        # La liveness probe del recolector revisa que este archivo se actualice.
        Path(self._settings.heartbeat_file).write_text(str(time.time()))

    async def _run(self, interval: int, cycle: Callable[[], Awaitable[None]]) -> None:
        while True:
            await cycle()
            # El latido indica que el ciclo sigue girando, no que la fuente
            # respondió: si GitHub se cae, reiniciar el pod no lo arregla. Eso
            # lo avisan meta.sync y las alarmas de CloudWatch.
            self._beat()
            await asyncio.sleep(interval)

    async def restore_sync_status(self) -> None:
        """Recupera el estado anterior al arrancar, para no perder el último éxito."""
        views = {
            "cluster",
            BUDGET_SNAPSHOT,
            *(deploys_snapshot_name(r) for r in self._settings.github_repos),
        }
        try:
            previous = await self._store.get_snapshots([SYNC_SNAPSHOT])
        except Exception:
            logger.exception("No se pudo leer el estado anterior de la sincronización")
            return
        if SYNC_SNAPSHOT in previous:
            # Solo las vistas que siguen configuradas (un repo retirado se olvida).
            self.sync_status = {
                view: status
                for view, status in previous[SYNC_SNAPSHOT].payload.items()
                if view in views
            }

    async def run_forever(self) -> None:
        self._beat()
        await self.restore_sync_status()
        loops = [
            self._run(self._settings.cluster_refresh_seconds, self.sync_cluster),
            self._run(self._settings.github_refresh_seconds, self.sync_deploys),
        ]
        if self._budgets is not None:
            loops.append(self._run(self._settings.budget_refresh_seconds, self.sync_budget))
        await asyncio.gather(*loops)


def describe_error(exc: Exception) -> str:
    """Resumen corto del error, sin detalles internos (va en las respuestas)."""
    if isinstance(exc, httpx.HTTPStatusError):
        url = exc.request.url
        return f"{url.host} respondió HTTP {exc.response.status_code} en {url.path}"
    if isinstance(exc, httpx.TransportError):
        return f"Sin conexión con {exc.request.url.host}: {type(exc).__name__}"
    return f"{type(exc).__name__}: {exc}"[:200]


class CloudWatchMetrics:
    """Publica en CloudWatch si cada sincronización salió bien (1) o mal (0).

    Las alarmas cuentan los éxitos en una ventana y tratan la falta de datos
    como falla: también avisan si el recolector se muere o pierde permisos.
    """

    def __init__(self, namespace: str, environment: str, region: str) -> None:
        import boto3

        self._namespace = namespace
        self._environment = environment
        self._client = boto3.session.Session().client("cloudwatch", region_name=region)

    def _put(self, source: str, ok: bool) -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[
                {
                    "MetricName": "SyncSuccess",
                    "Dimensions": [
                        {"Name": "Environment", "Value": self._environment},
                        {"Name": "Source", "Value": source},
                    ],
                    "Value": 1 if ok else 0,
                    "Unit": "Count",
                }
            ],
        )

    async def record(self, source: str, ok: bool) -> None:
        await asyncio.to_thread(self._put, source, ok)


def _github_token(settings: Settings) -> str:
    if settings.github_token or not settings.github_token_secret_id:
        return settings.github_token
    import boto3

    client = boto3.session.Session().client("secretsmanager", region_name=settings.aws_region)
    try:
        secret = client.get_secret_value(SecretId=settings.github_token_secret_id)
    except client.exceptions.ResourceNotFoundException:
        # El secreto existe pero nadie ha guardado el token todavía. Se arranca
        # sin token en vez de quedar en un ciclo de reinicios; al guardarlo,
        # basta reiniciar el recolector.
        logger.warning("El secreto %s no tiene valor", settings.github_token_secret_id)
        return ""
    return secret["SecretString"].strip()


def build(settings: Settings) -> Collector:
    store = DynamoStore(settings.table_name, settings.aws_region, settings.dynamodb_endpoint)
    if settings.create_table:
        store.create_table_if_missing()

    cluster: ClusterSource
    if settings.cluster_source == "kubernetes":
        from app.sources.kubernetes import KubernetesSource

        cluster = KubernetesSource(
            settings.k8s_api_url,
            settings.k8s_token_file,
            settings.k8s_ca_file,
            settings.watch_namespaces,
        )
    else:
        from app.sources.sample import SampleClusterSource

        cluster = SampleClusterSource()

    deploys: DeploySource
    if settings.github_source == "github":
        from app.sources.github import GitHubSource

        token = _github_token(settings)
        if not token:
            logger.warning("Sin token de GitHub: solo repos públicos y 60 peticiones por hora")
        deploys = GitHubSource(token, settings.deploy_environments)
    else:
        from app.sources.sample import SampleDeploySource

        deploys = SampleDeploySource()

    metrics = None
    if settings.metrics_namespace:
        metrics = CloudWatchMetrics(
            settings.metrics_namespace, settings.environment, settings.aws_region
        )

    budgets: BudgetSource | None = None
    if settings.budget_source == "aws":
        from app.sources.aws_budgets import AwsBudgetSource

        budgets = AwsBudgetSource()
    elif settings.budget_source == "sample":
        from app.sources.sample import SampleBudgetSource

        budgets = SampleBudgetSource()

    return Collector(settings, store, cluster, deploys, metrics, budgets)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    logger.info(
        "Recolector iniciado: repos=%s namespaces=%s",
        ",".join(settings.github_repos),
        ",".join(settings.watch_namespaces),
    )
    asyncio.run(build(settings).run_forever())


if __name__ == "__main__":
    main()
