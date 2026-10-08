"""nelua-api: despliegues (GitHub) y estado de los servicios (Kubernetes).

La API no consulta fuentes externas. Lee las fotos que deja el recolector en
DynamoDB y responde desde memoria; por eso escala a 10.000 RPS sin trasladarle
esa carga a GitHub, a Kubernetes ni a la base de datos.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from app.auth import ApiKeyStore, require_api_key
from app.collector import deploys_snapshot_name
from app.config import Settings, load_settings
from app.deploys import WINDOWS
from app.errors import ApiError, register_error_handlers
from app.models import DeployList, DeploymentList, DeployStatsList, Problem
from app.reader import SnapshotReader
from app.store import DynamoStore, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

REQUESTS = Counter("http_requests_total", "Total de peticiones HTTP", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Duración de las peticiones HTTP en segundos",
    ["method", "path"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)

# Antigüedad de cada foto. Si crece sin parar, el recolector dejó de funcionar:
# es la señal para el SLO de frescura.
SNAPSHOT_AGE = Gauge(
    "snapshot_age_seconds", "Segundos desde que el recolector tomó la foto", ["snapshot"]
)

# Rutas que no se miden: las llaman Kubernetes y Prometheus, no los clientes.
UNMEASURED_PATHS = {"/healthz", "/readyz", "/metrics"}

ERROR_RESPONSES = {
    401: {"model": Problem, "description": "Falta la API key o no es válida"},
    422: {"model": Problem, "description": "Parámetros inválidos"},
    503: {"model": Problem, "description": "Aún no hay datos recolectados"},
}

# owner/repo de GitHub: letras, números, guiones, puntos y guion bajo.
NAME_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = store or DynamoStore(
        settings.table_name, settings.aws_region, settings.dynamodb_endpoint
    )
    reader = SnapshotReader(store, settings.github_repos, settings.snapshot_refresh_seconds)
    api_keys = ApiKeyStore(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [
            asyncio.create_task(reader.refresh_forever()),
            asyncio.create_task(api_keys.refresh_forever()),
        ]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="nelua-api",
        summary="Despliegues de GitHub y estado de los servicios en Kubernetes",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.api_keys = api_keys
    app.state.reader = reader
    register_error_handlers(app)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        # Se usa la plantilla de la ruta y no la URL real, para que cada repo
        # consultado no genere una serie nueva en Prometheus.
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        if path not in UNMEASURED_PATHS:
            REQUESTS.labels(request.method, path, response.status_code).inc()
            LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    def repo_snapshot(owner: str, repo: str):
        full = f"{owner}/{repo}"
        if full not in reader.repos:
            raise ApiError(404, f"El repositorio '{full}' no está monitoreado. Ver GET /v1/repos")
        snapshot = reader.deploys(full)
        if snapshot is None:
            raise ApiError(503, f"Aún no hay datos de despliegues de '{full}'")
        return snapshot

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=ERROR_RESPONSES)

    @v1.get("/repos", tags=["despliegues"])
    def list_repos():
        """Repositorios de GitHub que se monitorean, con la frescura de sus datos."""
        return JSONResponse(
            {
                "data": [
                    {"repo": repo, "meta": reader.meta(deploys_snapshot_name(repo))}
                    for repo in reader.repos
                ]
            }
        )

    @v1.get(
        "/repos/{owner}/{repo}/deploys",
        response_model=DeployList,
        tags=["despliegues"],
        responses={404: {"model": Problem, "description": "Repositorio no monitoreado"}},
    )
    def list_deploys(
        owner: str = Path(pattern=NAME_PATTERN),
        repo: str = Path(pattern=NAME_PATTERN),
        environment: str | None = Query(default=None, description="Filtra por ambiente"),
        limit: int = Query(default=20, ge=1, le=100, description="Máximo de resultados"),
    ):
        """Últimos despliegues del repositorio, del más reciente al más antiguo."""
        snapshot = repo_snapshot(owner, repo)
        items = [
            d
            for d in snapshot.payload["latest"]
            if environment is None or d["environment"] == environment
        ]
        return JSONResponse(
            {"data": items[:limit], "meta": reader.meta(deploys_snapshot_name(f"{owner}/{repo}"))}
        )

    @v1.get(
        "/repos/{owner}/{repo}/deploys/stats",
        response_model=DeployStatsList,
        tags=["despliegues"],
        responses={404: {"model": Problem, "description": "Repositorio no monitoreado"}},
    )
    def deploy_stats(
        owner: str = Path(pattern=NAME_PATTERN),
        repo: str = Path(pattern=NAME_PATTERN),
        environment: str | None = Query(
            default=None, description="Ambiente, o 'all' para el total. Sin filtro: todos"
        ),
        days: int = Query(default=30, description=f"Ventana en días, una de {WINDOWS}"),
    ):
        """Tasa de éxito, frecuencia y duración de los despliegues en una ventana de días."""
        if days not in WINDOWS:
            raise ApiError(422, f"days debe ser uno de {list(WINDOWS)}")
        snapshot = repo_snapshot(owner, repo)
        items = [
            s
            for s in snapshot.payload["stats"]
            if s["window_days"] == days and (environment is None or s["environment"] == environment)
        ]
        return JSONResponse(
            {"data": items, "meta": reader.meta(deploys_snapshot_name(f"{owner}/{repo}"))}
        )

    @v1.get("/deployments", response_model=DeploymentList, tags=["clúster"])
    def list_deployments(
        namespace: str | None = Query(default=None, description="Filtra por namespace"),
        name: str | None = Query(default=None, description="Filtra por nombre"),
        status: Literal["healthy", "progressing", "degraded", "unavailable", "scaled_down"]
        | None = Query(default=None, description="Filtra por estado"),
    ):
        """Estado de los deployments del clúster por namespace: réplicas, versión,
        pods, autoescalado e historial de revisiones (incluye los rollbacks)."""
        snapshot = reader.get("cluster")
        if snapshot is None:
            raise ApiError(503, "Aún no se ha recolectado el estado del clúster")
        items = [
            d
            for d in snapshot.payload
            if (namespace is None or d["namespace"] == namespace)
            and (name is None or d["name"] == name)
            and (status is None or d["status"] == status)
        ]
        return JSONResponse({"data": items, "meta": reader.meta("cluster")})

    app.include_router(v1)

    # ---------- Endpoints de operación (sin API key) ----------
    @app.get("/healthz", tags=["operación"])
    def healthz():
        """Liveness: el proceso está vivo. Si falla, Kubernetes reinicia el contenedor."""
        return {"status": "ok", "version": settings.app_version}

    @app.get("/readyz", tags=["operación"], responses={503: {"model": Problem}})
    def readyz():
        """Readiness: pudo leer el almacén al menos una vez y tiene llaves para autenticar.

        No depende de GitHub ni de Kubernetes: si una fuente falla, la API sigue
        sirviendo la última foto.
        """
        if not reader.loaded:
            raise ApiError(503, "Aún no se ha podido leer el almacén")
        if not api_keys.loaded:
            raise ApiError(503, "No hay API keys cargadas")
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        now = datetime.now(UTC)
        for name in reader.views:
            snapshot = reader.get(name)
            if snapshot is not None:
                SNAPSHOT_AGE.labels(name).set((now - snapshot.collected_at).total_seconds())
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
