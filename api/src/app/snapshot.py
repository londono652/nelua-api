"""La "foto" en memoria que sirve la API, y el recolector que la actualiza.

Idea central del diseño: las peticiones NUNCA consultan a Kubernetes ni a AWS.
Un recolector en segundo plano refresca la foto cada pocos segundos y los
endpoints responden desde memoria. Así, 10 o 10.000 peticiones por segundo
generan la misma carga sobre las fuentes: una consulta por pod por ciclo.

Si una fuente falla, se conserva la última foto válida y se marca como
desactualizada ("stale"), en lugar de dejar de responder.
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter as PromCounter
from prometheus_client import Gauge

from app.alerts import budget_alerts, cluster_alerts, sort_alerts, stale_alert
from app.models import Alert, Budget, ClusterState
from app.sources.base import BudgetSource, ClusterSource

logger = logging.getLogger("nelua.collector")

COLLECTIONS = PromCounter(
    "collector_runs_total", "Recolecciones ejecutadas, por fuente y resultado", ["source", "result"]
)
LAST_SUCCESS = Gauge(
    "collector_last_success_timestamp_seconds",
    "Momento (epoch) de la última recolección exitosa, por fuente",
    ["source"],
)

ACTIVE_ALERTS = Gauge("active_alerts", "Alertas activas, por severidad", ["severity"])

# Una foto se considera desactualizada si lleva más de 3 ciclos sin renovarse.
STALE_AFTER_CYCLES = 3


class Section:
    """Una parte de la foto (clúster o presupuesto) con su antigüedad."""

    def __init__(self, source: str, refresh_seconds: int) -> None:
        self.source = source
        self.refresh_seconds = refresh_seconds
        self.collected_at: datetime | None = None

    def mark(self) -> None:
        self.collected_at = datetime.now(UTC)

    def meta(self) -> dict[str, Any]:
        if self.collected_at is None:
            return {"collected_at": None, "stale": True, "source": self.source}
        age = (datetime.now(UTC) - self.collected_at).total_seconds()
        return {
            "collected_at": self.collected_at.isoformat().replace("+00:00", "Z"),
            "stale": age > self.refresh_seconds * STALE_AFTER_CYCLES,
            "source": self.source,
        }


class Snapshot:
    """Vistas ya serializadas, listas para responder sin trabajo por petición."""

    def __init__(
        self,
        environment: str,
        cluster: Section,
        budget: Section,
        alert_pod_restarts: int = 3,
        alert_min_zones: int = 2,
    ) -> None:
        self.environment = environment
        self.cluster = cluster
        self.budget = budget
        self._alert_pod_restarts = alert_pod_restarts
        self._alert_min_zones = alert_min_zones
        self._cluster_alerts: list[Alert] = []
        self._budget_alerts: list[Alert] = []
        self.deployments: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.budgets: list[dict[str, Any]] = []

    @property
    def ready(self) -> bool:
        return self.cluster.collected_at is not None

    def set_cluster(self, state: ClusterState) -> None:
        # Historial por deployment: la revisión más alta es la más reciente.
        history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for revision in sorted(state.revisions, key=lambda r: r.revision, reverse=True):
            history.setdefault((revision.namespace, revision.deployment), []).append(
                revision.model_dump(mode="json")
            )
        self.deployments = [
            {**d.model_dump(mode="json"), "history": history.get((d.namespace, d.name), [])}
            for d in state.deployments
        ]
        self.nodes = [node.model_dump(mode="json") for node in state.nodes]

        self._cluster_alerts = cluster_alerts(
            state, self._alert_pod_restarts, self._alert_min_zones
        )
        self.cluster.mark()
        self._publish_alert_metrics()

    def set_budgets(self, budgets: list[Budget]) -> None:
        self.budgets = [budget.model_dump(mode="json") for budget in budgets]
        self._budget_alerts = budget_alerts(budgets)
        self.budget.mark()
        self._publish_alert_metrics()

    def _publish_alert_metrics(self) -> None:
        counts = Counter(alert.severity for alert in self._cluster_alerts + self._budget_alerts)
        for severity in ("critical", "warning", "info"):
            ACTIVE_ALERTS.labels(severity).set(counts.get(severity, 0))

    def alerts(self) -> list[dict[str, Any]]:
        """Alertas activas, las más graves primero.

        Las de datos desactualizados se evalúan al responder, porque dependen
        de cuánto tiempo ha pasado desde la última recolección exitosa.
        """
        alerts = self._cluster_alerts + self._budget_alerts
        if self.cluster.meta()["stale"]:
            alerts = [*alerts, stale_alert("cluster", self.cluster.source)]
        if self.budget.source != "none" and self.budget.meta()["stale"]:
            alerts = [*alerts, stale_alert("budget", self.budget.source)]
        return [alert.model_dump(mode="json") for alert in sort_alerts(alerts)]

    def summary(self) -> dict[str, Any]:
        severity = {"ok": 0, "warning": 1, "exceeded": 2}
        worst = max((b["status"] for b in self.budgets), key=severity.get, default=None)
        alerts = self.alerts()
        return {
            "environment": self.environment,
            "nodes": {
                "total": len(self.nodes),
                "ready": sum(1 for node in self.nodes if node["ready"]),
                "by_zone": dict(Counter(node["zone"] or "unknown" for node in self.nodes)),
                "by_capacity_type": dict(
                    Counter(node["capacity_type"] or "unknown" for node in self.nodes)
                ),
                "items": self.nodes,
            },
            "deployments": {
                "total": len(self.deployments),
                "by_status": dict(Counter(d["status"] for d in self.deployments)),
            },
            "budget": {
                "status": worst,
                "budgets": len(self.budgets),
                "highest_percent_used": max(
                    (b["percent_used"] for b in self.budgets), default=None
                ),
            },
            "alerts": {
                "total": len(alerts),
                "by_severity": dict(Counter(alert["severity"] for alert in alerts)),
            },
        }


class Collector:
    def __init__(
        self,
        snapshot: Snapshot,
        cluster_source: ClusterSource,
        budget_source: BudgetSource | None,
    ) -> None:
        self._snapshot = snapshot
        self._cluster_source = cluster_source
        self._budget_source = budget_source
        self._tasks: list[asyncio.Task] = []

    async def collect_cluster(self) -> None:
        self._snapshot.set_cluster(await self._cluster_source.collect())

    async def collect_budgets(self) -> None:
        if self._budget_source is not None:
            self._snapshot.set_budgets(await self._budget_source.collect())

    async def _run(self, source: str, interval: int, collect) -> None:
        while True:
            try:
                await collect()
                COLLECTIONS.labels(source, "success").inc()
                LAST_SUCCESS.labels(source).set(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                COLLECTIONS.labels(source, "error").inc()
                logger.exception("Falló la recolección de %s; se conserva la foto anterior", source)
            await asyncio.sleep(interval)

    def start(self) -> None:
        cluster, budget = self._snapshot.cluster, self._snapshot.budget
        self._tasks.append(
            asyncio.create_task(
                self._run(cluster.source, cluster.refresh_seconds, self.collect_cluster)
            )
        )
        if self._budget_source is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._run(budget.source, budget.refresh_seconds, self.collect_budgets)
                )
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
