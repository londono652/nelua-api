"""Modelos: lo que la API expone sobre despliegues y sobre el clúster."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

DeploymentStatus = Literal["healthy", "progressing", "degraded", "unavailable", "scaled_down"]
DeployStatus = Literal["success", "failure", "in_progress"]


# ---------- Despliegues (GitHub) ----------


class Deploy(BaseModel):
    """Un despliegue registrado en GitHub por el pipeline (GitHub Deployments)."""

    id: int
    environment: str
    status: DeployStatus
    # Estado tal como lo reporta GitHub (success, failure, error, inactive, ...).
    github_state: str
    sha: str
    ref: str
    creator: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    run_url: str | None = None

    @property
    def final(self) -> bool:
        return self.status != "in_progress"


class DeployStats(BaseModel):
    """Métricas de despliegue de un ambiente en una ventana de tiempo."""

    environment: str
    window_days: int
    total: int
    succeeded: int
    failed: int
    in_progress: int
    # Porcentaje de despliegues terminados que salieron bien (0 a 100).
    success_rate: float | None
    # Lo inverso, con el nombre que usan las métricas DORA.
    change_failure_rate: float | None
    deploys_per_day: float
    avg_duration_seconds: int | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


# ---------- Clúster (Kubernetes) ----------


class Replicas(BaseModel):
    desired: int
    ready: int
    updated: int
    available: int


class Autoscaling(BaseModel):
    min_replicas: int
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    cpu_target_percent: int | None = None
    cpu_current_percent: int | None = None


class Pod(BaseModel):
    name: str
    phase: str
    ready: bool
    restarts: int
    node: str | None = None
    started_at: datetime | None = None


class RevisionReplicas(BaseModel):
    desired: int
    ready: int


class Revision(BaseModel):
    """Una revisión del deployment: cada versión desplegada deja una."""

    namespace: str
    deployment: str
    revision: int
    version: str
    image: str
    created_at: datetime | None = None
    # Es la revisión que está sirviendo ahora.
    active: bool
    # La versión ya había estado desplegada y volvió a activarse (un rollback).
    reactivated: bool = False
    replicas: RevisionReplicas


class Deployment(BaseModel):
    namespace: str
    name: str
    status: DeploymentStatus
    replicas: Replicas
    image: str
    version: str
    updated_at: datetime | None = None
    autoscaling: Autoscaling | None = None
    pods: list[Pod] = []


class DeploymentView(Deployment):
    history: list[Revision] = []


class ClusterState(BaseModel):
    deployments: list[Deployment]
    revisions: list[Revision] = []


# ---------- Respuestas ----------


class SyncStatus(BaseModel):
    """Cómo le fue al recolector en sus últimos intentos con esta fuente."""

    last_attempt_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    # Por qué falló el último intento (None si salió bien).
    error: str | None


class Meta(BaseModel):
    """Qué tan reciente es el dato: cuándo lo tomó el recolector y si está viejo."""

    collected_at: datetime | None
    stale: bool
    source: str
    sync: SyncStatus | None = None


class Problem(BaseModel):
    """Formato único de error (RFC 9457, "Problem Details for HTTP APIs")."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None


class DeployList(BaseModel):
    data: list[Deploy]
    meta: Meta


class DeployStatsList(BaseModel):
    data: list[DeployStats]
    meta: Meta


class DeploymentList(BaseModel):
    data: list[DeploymentView]
    meta: Meta
