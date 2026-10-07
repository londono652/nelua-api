"""Reglas de alerta: convierten la foto del clúster y del presupuesto en una
lista de problemas activos.

Son funciones puras (sin red ni estado): reciben los datos ya recolectados y
devuelven las alertas. Por eso se prueban sin clúster y no agregan carga.
"""

from app.models import Alert, AlertResource, Budget, ClusterState, Severity

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _alert(
    severity: Severity,
    code: str,
    kind: str,
    name: str,
    message: str,
    namespace: str | None = None,
    since=None,
) -> Alert:
    location = f"{namespace}/{name}" if namespace else name
    return Alert(
        id=f"{code}:{kind.lower()}:{location}",
        severity=severity,
        code=code,
        resource=AlertResource(kind=kind, name=name, namespace=namespace),
        message=message,
        since=since,
    )


def cluster_alerts(state: ClusterState, pod_restarts: int, min_zones: int) -> list[Alert]:
    alerts: list[Alert] = []

    for d in state.deployments:
        ready = f"{d.replicas.ready} de {d.replicas.desired} réplicas listas"
        if d.status == "unavailable":
            alerts.append(
                _alert(
                    "critical",
                    "deployment_unavailable",
                    "Deployment",
                    d.name,
                    f"El servicio no está disponible: {ready}",
                    d.namespace,
                    d.updated_at,
                )
            )
        elif d.status == "degraded":
            alerts.append(
                _alert(
                    "warning",
                    "deployment_degraded",
                    "Deployment",
                    d.name,
                    f"El servicio está degradado: {ready}",
                    d.namespace,
                    d.updated_at,
                )
            )

        scaling = d.autoscaling
        if scaling and scaling.max_replicas > scaling.min_replicas:
            if scaling.current_replicas >= scaling.max_replicas:
                alerts.append(
                    _alert(
                        "warning",
                        "autoscaling_at_max",
                        "Deployment",
                        d.name,
                        f"El autoescalador llegó a su máximo ({scaling.max_replicas} réplicas): "
                        "no queda margen para absorber más tráfico",
                        d.namespace,
                    )
                )

        for pod in d.pods:
            if pod.restarts >= pod_restarts:
                alerts.append(
                    _alert(
                        "warning",
                        "pod_restarting",
                        "Pod",
                        pod.name,
                        f"El pod se ha reiniciado {pod.restarts} veces",
                        d.namespace,
                        pod.started_at,
                    )
                )

    for node in state.nodes:
        if not node.ready:
            alerts.append(
                _alert("critical", "node_not_ready", "Node", node.name, "El nodo no está listo")
            )

    zones = {node.zone for node in state.nodes if node.ready and node.zone}
    if state.nodes and len(zones) < min_zones:
        alerts.append(
            _alert(
                "warning",
                "low_zone_redundancy",
                "Cluster",
                "nodes",
                f"Hay nodos listos en {len(zones)} zona(s); se esperan al menos {min_zones} "
                "para tolerar la caída de una zona",
            )
        )

    for event in state.events:
        alerts.append(
            _alert(
                "warning",
                "kubernetes_warning",
                event.kind,
                event.name,
                f"{event.reason}: {event.message}",
                event.namespace,
                event.last_seen,
            )
        )
        # Varios motivos sobre el mismo recurso no deben pisarse entre sí.
        alerts[-1].id += f":{event.reason}"

    return alerts


def budget_alerts(budgets: list[Budget]) -> list[Alert]:
    alerts: list[Alert] = []
    for budget in budgets:
        limit, spent = budget.limit, budget.actual_spend
        if budget.status == "exceeded":
            alerts.append(
                _alert(
                    "critical",
                    "budget_exceeded",
                    "Budget",
                    budget.name,
                    f"El gasto ({spent.amount:.2f} {spent.unit}) superó el presupuesto "
                    f"de {limit.amount:.2f} {limit.unit}",
                )
            )
        elif budget.status == "warning":
            forecast = budget.forecasted_spend
            if forecast and forecast.amount > limit.amount:
                detail = (
                    f"el pronóstico del periodo ({forecast.amount:.2f} {forecast.unit}) "
                    f"supera el límite de {limit.amount:.2f} {limit.unit}"
                )
            else:
                detail = f"va en {budget.percent_used:.1f} % del límite"
            alerts.append(
                _alert(
                    "warning",
                    "budget_at_risk",
                    "Budget",
                    budget.name,
                    f"Presupuesto en riesgo: {detail}",
                )
            )
    return alerts


def stale_alert(section: str, source: str) -> Alert:
    """La fuente dejó de responder: lo que muestra la API puede no ser lo actual."""
    return _alert(
        "warning",
        "data_stale",
        "Source",
        section,
        f"Los datos de '{section}' (fuente: {source}) están desactualizados o aún no se han "
        "podido recolectar",
    )


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (SEVERITY_ORDER[a.severity], a.code, a.id))
