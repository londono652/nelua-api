"""Reglas de alerta: qué situación del clúster o del presupuesto dispara cada una."""

from datetime import UTC, datetime, timedelta

from app.alerts import budget_alerts, cluster_alerts, sort_alerts
from app.models import (
    Autoscaling,
    Budget,
    ClusterEvent,
    ClusterState,
    DeploymentDetail,
    Money,
    Node,
    Pod,
    Replicas,
)
from app.snapshot import Collector, Section, Snapshot
from app.sources.sample import SampleBudgetSource, SampleClusterSource


def deployment(status="healthy", ready=3, pods=(), autoscaling=None) -> DeploymentDetail:
    return DeploymentDetail(
        namespace="nelua-api",
        name="nelua-api",
        status=status,
        replicas=Replicas(desired=3, ready=ready, updated=3, available=ready),
        image="repo/nelua-api:abc",
        version="abc",
        autoscaling=autoscaling,
        pods=list(pods),
    )


def node(name, zone, ready=True) -> Node:
    return Node(name=name, ready=ready, zone=zone)


THREE_ZONES = [node("a", "us-east-2a"), node("b", "us-east-2b"), node("c", "us-east-2c")]


def budget(status, percent, forecast=None) -> Budget:
    return Budget(
        name="mensual",
        period="monthly",
        limit=Money(amount=50, unit="USD"),
        actual_spend=Money(amount=percent / 2, unit="USD"),
        forecasted_spend=Money(amount=forecast, unit="USD") if forecast else None,
        percent_used=percent,
        status=status,
    )


def codes(state: ClusterState, restarts=3, zones=2) -> list[tuple[str, str]]:
    return [(a.severity, a.code) for a in cluster_alerts(state, restarts, zones)]


def test_healthy_cluster_has_no_alerts():
    state = ClusterState(deployments=[deployment()], nodes=THREE_ZONES)
    assert codes(state) == []


def test_unavailable_deployment_is_critical_and_degraded_is_warning():
    state = ClusterState(
        deployments=[deployment("unavailable", ready=0), deployment("degraded", ready=2)],
        nodes=THREE_ZONES,
    )
    assert codes(state) == [
        ("critical", "deployment_unavailable"),
        ("warning", "deployment_degraded"),
    ]
    assert "0 de 3" in cluster_alerts(state, 3, 2)[0].message


def test_a_rollout_in_progress_is_not_an_alert():
    state = ClusterState(deployments=[deployment("progressing", ready=2)], nodes=THREE_ZONES)
    assert codes(state) == []


def test_pod_restarts_alert_respects_the_threshold():
    pods = [
        Pod(name="p1", phase="Running", ready=True, restarts=2),
        Pod(name="p2", phase="Running", ready=True, restarts=5),
    ]
    state = ClusterState(deployments=[deployment(pods=pods)], nodes=THREE_ZONES)
    alerts = cluster_alerts(state, 3, 2)
    assert [(a.code, a.resource.name) for a in alerts] == [("pod_restarting", "p2")]
    assert codes(state, restarts=10) == []


def test_autoscaler_at_max_means_no_headroom():
    at_max = Autoscaling(min_replicas=3, max_replicas=30, current_replicas=30, desired_replicas=30)
    below = Autoscaling(min_replicas=3, max_replicas=30, current_replicas=12, desired_replicas=12)
    fixed = Autoscaling(min_replicas=2, max_replicas=2, current_replicas=2, desired_replicas=2)

    def run(autoscaling):
        return codes(
            ClusterState(deployments=[deployment(autoscaling=autoscaling)], nodes=THREE_ZONES)
        )

    assert run(at_max) == [("warning", "autoscaling_at_max")]
    assert run(below) == []
    assert run(fixed) == []  # tamaño fijo: estar en el máximo es lo normal


def test_node_not_ready_is_critical():
    state = ClusterState(deployments=[], nodes=[*THREE_ZONES, node("d", "us-east-2a", False)])
    assert codes(state) == [("critical", "node_not_ready")]


def test_nodes_in_a_single_zone_put_high_availability_at_risk():
    one_zone = ClusterState(
        deployments=[], nodes=[node("a", "us-east-2a"), node("b", "us-east-2a")]
    )
    assert codes(one_zone) == [("warning", "low_zone_redundancy")]
    assert codes(one_zone, zones=1) == []
    # Un nodo no listo no cuenta como zona cubierta.
    degraded = ClusterState(
        deployments=[], nodes=[node("a", "us-east-2a"), node("b", "us-east-2b", False)]
    )
    assert ("warning", "low_zone_redundancy") in codes(degraded)


def test_kubernetes_warning_events_become_alerts():
    seen = datetime.now(UTC)
    events = [
        ClusterEvent(namespace="nelua-api", kind="Pod", name="p1", reason="BackOff", message="x"),
        ClusterEvent(
            namespace="nelua-api",
            kind="Pod",
            name="p1",
            reason="Unhealthy",
            message="y",
            last_seen=seen,
        ),
    ]
    alerts = cluster_alerts(ClusterState(deployments=[], nodes=THREE_ZONES, events=events), 3, 2)
    assert [a.code for a in alerts] == ["kubernetes_warning"] * 2
    assert len({a.id for a in alerts}) == 2  # cada motivo es una alerta distinta
    assert alerts[1].message == "Unhealthy: y"
    assert alerts[1].since == seen


def test_budget_alerts():
    assert budget_alerts([budget("ok", 30, 40)]) == []

    (exceeded,) = budget_alerts([budget("exceeded", 104)])
    assert (exceeded.severity, exceeded.code) == ("critical", "budget_exceeded")

    (by_forecast,) = budget_alerts([budget("warning", 40, 61)])
    assert (by_forecast.severity, by_forecast.code) == ("warning", "budget_at_risk")
    assert "pronóstico" in by_forecast.message

    (by_spend,) = budget_alerts([budget("warning", 85, 45)])
    assert "85.0 %" in by_spend.message


def test_alerts_are_sorted_by_severity():
    state = ClusterState(
        deployments=[deployment("degraded", ready=2)],
        nodes=[*THREE_ZONES, node("d", "us-east-2a", False)],
    )
    alerts = sort_alerts(cluster_alerts(state, 3, 2) + budget_alerts([budget("exceeded", 120)]))
    assert [a.severity for a in alerts] == ["critical", "critical", "warning"]


async def test_stale_data_raises_an_alert():
    snapshot = Snapshot("test", Section("sample", 15), Section("sample", 900))
    collector = Collector(snapshot, SampleClusterSource(), SampleBudgetSource())
    await collector.collect_cluster()
    await collector.collect_budgets()
    assert "data_stale" not in {a["code"] for a in snapshot.alerts()}

    snapshot.cluster.collected_at = datetime.now(UTC) - timedelta(seconds=60)
    stale = [a for a in snapshot.alerts() if a["code"] == "data_stale"]
    assert [a["resource"]["name"] for a in stale] == ["cluster"]
    assert snapshot.summary()["alerts"]["total"] == 3


async def test_no_budget_source_is_not_an_alert():
    snapshot = Snapshot("test", Section("sample", 15), Section("none", 900))
    await Collector(snapshot, SampleClusterSource(), None).collect_cluster()
    assert "data_stale" not in {a["code"] for a in snapshot.alerts()}
