"""Modelos del dominio: lo que la API expone sobre el clúster y el presupuesto."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

DeploymentStatus = Literal["healthy", "progressing", "degraded", "unavailable", "scaled_down"]
BudgetStatus = Literal["ok", "warning", "exceeded"]
Severity = Literal["critical", "warning", "info"]


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


class Deployment(BaseModel):
    namespace: str
    name: str
    status: DeploymentStatus
    replicas: Replicas
    image: str
    version: str
    updated_at: datetime | None = None
    autoscaling: Autoscaling | None = None


class DeploymentDetail(Deployment):
    pods: list[Pod] = []


class Node(BaseModel):
    name: str
    ready: bool
    zone: str | None = None
    instance_type: str | None = None
    architecture: str | None = None
    capacity_type: str | None = None
    node_pool: str | None = None
    kubelet_version: str | None = None
    created_at: datetime | None = None


class Money(BaseModel):
    amount: float
    unit: str


class Budget(BaseModel):
    name: str
    period: str
    limit: Money
    actual_spend: Money
    forecasted_spend: Money | None = None
    percent_used: float
    status: BudgetStatus


class RevisionReplicas(BaseModel):
    desired: int
    ready: int


class Revision(BaseModel):
    """Un despliegue: cada cambio de versión deja una revisión en el clúster."""

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


class ClusterEvent(BaseModel):
    """Evento de tipo Warning reportado por Kubernetes."""

    namespace: str
    kind: str
    name: str
    reason: str
    message: str
    count: int = 1
    last_seen: datetime | None = None


class AlertResource(BaseModel):
    kind: str
    name: str
    namespace: str | None = None


class Alert(BaseModel):
    id: str
    severity: Severity
    code: str
    resource: AlertResource
    message: str
    since: datetime | None = None


class ClusterState(BaseModel):
    """Resultado de una recolección completa del clúster."""

    deployments: list[DeploymentDetail]
    nodes: list[Node]
    revisions: list[Revision] = []
    events: list[ClusterEvent] = []


# ---------- Respuestas ----------


class Meta(BaseModel):
    """Qué tan fresco es el dato: cuándo se tomó y si está desactualizado."""

    collected_at: datetime | None
    stale: bool
    source: str


class Problem(BaseModel):
    """Formato único de error (RFC 9457, "Problem Details for HTTP APIs")."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None


class DeploymentView(DeploymentDetail):
    """Un servicio completo: estado, pods, autoescalado e historial de despliegues."""

    history: list[Revision] = []


class DeploymentList(BaseModel):
    data: list[DeploymentView]
    meta: Meta


class BudgetList(BaseModel):
    data: list[Budget]
    meta: Meta


class SummaryMeta(BaseModel):
    cluster: Meta
    budget: Meta


class AlertList(BaseModel):
    data: list[Alert]
    meta: SummaryMeta


class AlertSummary(BaseModel):
    total: int
    by_severity: dict[str, int]


class NodeSummary(BaseModel):
    total: int
    ready: int
    by_zone: dict[str, int]
    by_capacity_type: dict[str, int]
    items: list[Node]


class DeploymentSummary(BaseModel):
    total: int
    by_status: dict[str, int]


class BudgetSummary(BaseModel):
    status: BudgetStatus | None
    budgets: int
    highest_percent_used: float | None


class Summary(BaseModel):
    environment: str
    nodes: NodeSummary
    deployments: DeploymentSummary
    budget: BudgetSummary
    alerts: AlertSummary


class SummaryResponse(BaseModel):
    data: Summary
    meta: SummaryMeta
