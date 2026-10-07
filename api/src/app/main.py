"""nelua-api: estado de la infraestructura (clúster, despliegues, alertas y presupuesto)."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.auth import ApiKeyStore, require_api_key
from app.config import Settings, load_settings
from app.errors import ApiError, register_error_handlers
from app.models import (
    AlertList,
    BudgetList,
    DeploymentList,
    Problem,
    SummaryResponse,
)
from app.snapshot import Collector, Section, Snapshot
from app.sources.base import BudgetSource, ClusterSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

REQUESTS = Counter("http_requests_total", "Total de peticiones HTTP", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Duración de las peticiones HTTP en segundos",
    ["method", "path"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)

# Rutas que no se miden: las llaman Kubernetes y Prometheus, no los clientes.
UNMEASURED_PATHS = {"/healthz", "/readyz", "/metrics"}

ERROR_RESPONSES = {
    401: {"model": Problem, "description": "Falta la API key o no es válida"},
    422: {"model": Problem, "description": "Parámetros inválidos"},
    503: {"model": Problem, "description": "Aún no hay datos recolectados"},
}


def build_sources(settings: Settings) -> tuple[ClusterSource, BudgetSource | None]:
    """Elige las fuentes de datos según la configuración."""
    if settings.cluster_source == "kubernetes":
        from app.sources.kubernetes import KubernetesSource

        cluster: ClusterSource = KubernetesSource(
            settings.k8s_api_url,
            settings.k8s_token_file,
            settings.k8s_ca_file,
            settings.watch_namespaces,
            events_window_minutes=settings.events_window_minutes,
        )
    else:
        from app.sources.sample import SampleClusterSource

        cluster = SampleClusterSource()

    budget: BudgetSource | None
    if settings.budget_source == "aws":
        from app.sources.aws_budgets import AwsBudgetSource

        budget = AwsBudgetSource()
    elif settings.budget_source == "sample":
        from app.sources.sample import SampleBudgetSource

        budget = SampleBudgetSource()
    else:
        budget = None
    return cluster, budget


def create_app(
    settings: Settings | None = None,
    cluster_source: ClusterSource | None = None,
    budget_source: BudgetSource | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if cluster_source is None:
        cluster_source, budget_source = build_sources(settings)

    snapshot = Snapshot(
        environment=settings.environment,
        cluster=Section(cluster_source.name, settings.cluster_refresh_seconds),
        budget=Section(
            budget_source.name if budget_source else "none", settings.budget_refresh_seconds
        ),
        alert_pod_restarts=settings.alert_pod_restarts,
        alert_min_zones=settings.alert_min_zones,
    )
    collector = Collector(snapshot, cluster_source, budget_source)
    api_keys = ApiKeyStore(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        collector.start()
        keys_task = asyncio.create_task(api_keys.refresh_forever())
        yield
        keys_task.cancel()
        await collector.stop()

    app = FastAPI(
        title="nelua-api",
        summary="Estado de la infraestructura: clúster, despliegues, alertas y presupuesto de AWS",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.snapshot = snapshot
    app.state.api_keys = api_keys
    register_error_handlers(app)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        # Se usa la plantilla de la ruta y no la URL real, para que cada
        # deployment consultado no genere una serie nueva en Prometheus.
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        if path not in UNMEASURED_PATHS:
            REQUESTS.labels(request.method, path, response.status_code).inc()
            LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    def current_snapshot() -> Snapshot:
        if not snapshot.ready:
            raise ApiError(503, "Aún no se ha recolectado el estado del clúster")
        return snapshot

    # ---------- Endpoints de negocio (requieren API key) ----------
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=ERROR_RESPONSES)

    @v1.get("/summary", response_model=SummaryResponse, tags=["estado"])
    def get_summary():
        """Panorama en una llamada: nodos, servicios por estado, presupuesto y alertas."""
        snap = current_snapshot()
        return JSONResponse(
            {
                "data": snap.summary(),
                "meta": {"cluster": snap.cluster.meta(), "budget": snap.budget.meta()},
            }
        )

    @v1.get("/alerts", response_model=AlertList, tags=["estado"])
    def list_alerts(
        severity: Literal["critical", "warning", "info"] | None = Query(
            default=None, description="Filtra por severidad"
        ),
        namespace: str | None = Query(default=None, description="Filtra por namespace"),
    ):
        """Problemas activos ahora, los más graves primero.

        Se calculan con reglas sobre el estado del clúster y del presupuesto:
        servicios caídos o degradados, nodos no listos, pods reiniciándose,
        autoescalado al máximo, presupuesto en riesgo y eventos de Kubernetes.
        """
        snap = current_snapshot()
        items = [
            item
            for item in snap.alerts()
            if (severity is None or item["severity"] == severity)
            and (namespace is None or item["resource"]["namespace"] == namespace)
        ]
        return JSONResponse(
            {"data": items, "meta": {"cluster": snap.cluster.meta(), "budget": snap.budget.meta()}}
        )

    @v1.get("/deployments", response_model=DeploymentList, tags=["clúster"])
    def list_deployments(
        namespace: str | None = Query(default=None, description="Filtra por namespace"),
        name: str | None = Query(default=None, description="Filtra por nombre del servicio"),
        status: Literal["healthy", "progressing", "degraded", "unavailable", "scaled_down"]
        | None = Query(default=None, description="Filtra por estado"),
    ):
        """Servicios desplegados: estado, réplicas, versión, pods, autoescalado e
        historial de despliegues.

        El historial sale de las revisiones que guarda Kubernetes (las últimas
        10): indica cuál está activa y si es una versión anterior que volvió a
        activarse, que es como se ve un rollback.
        """
        snap = current_snapshot()
        items = [
            item
            for item in snap.deployments
            if (namespace is None or item["namespace"] == namespace)
            and (name is None or item["name"] == name)
            and (status is None or item["status"] == status)
        ]
        return JSONResponse({"data": items, "meta": snap.cluster.meta()})

    @v1.get("/budget", response_model=BudgetList, tags=["costos"])
    def get_budget():
        """Presupuestos de la cuenta de AWS: límite, gasto, pronóstico y estado."""
        snap = current_snapshot()
        return JSONResponse({"data": snap.budgets, "meta": snap.budget.meta()})

    app.include_router(v1)

    # ---------- Endpoints de operación (sin API key) ----------
    @app.get("/healthz", tags=["operación"])
    def healthz():
        """Liveness: el proceso está vivo. Si falla, Kubernetes reinicia el contenedor."""
        return {"status": "ok", "version": settings.app_version}

    @app.get("/readyz", tags=["operación"], responses={503: {"model": Problem}})
    def readyz():
        """Readiness: hay datos que servir y llaves para autenticar.

        No consulta a Kubernetes ni a AWS en cada llamada: si una fuente tiene
        un fallo momentáneo, el pod sigue sirviendo la última foto.
        """
        if not snapshot.ready:
            raise ApiError(503, "Aún no se ha recolectado el estado del clúster")
        if not api_keys.loaded:
            raise ApiError(503, "No hay API keys cargadas")
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
