"""Traducción de las respuestas de Kubernetes y de AWS Budgets al modelo de la API."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.auth import parse_keys
from app.sources.aws_budgets import AwsBudgetSource, budget_status, parse_budget
from app.sources.kubernetes import (
    KubernetesSource,
    deployment_status,
    parse_events,
    parse_revisions,
    split_image,
)

IMAGE = "123.dkr.ecr.us-east-2.amazonaws.com/nelua-api"

DEPLOYMENT = {
    "metadata": {
        "name": "nelua-api",
        "namespace": "nelua-api",
        "annotations": {"deployment.kubernetes.io/revision": "5"},
    },
    "spec": {
        "replicas": 3,
        "selector": {"matchLabels": {"app.kubernetes.io/name": "nelua-api"}},
        "template": {
            "spec": {
                "containers": [{"image": "123.dkr.ecr.us-east-2.amazonaws.com/nelua-api:abc123"}]
            }
        },
    },
    "status": {
        "readyReplicas": 3,
        "updatedReplicas": 3,
        "availableReplicas": 3,
        "conditions": [
            {"type": "Available", "status": "True"},
            {
                "type": "Progressing",
                "status": "True",
                "reason": "NewReplicaSetAvailable",
                "lastUpdateTime": "2026-10-05T14:32:00Z",
            },
        ],
    },
}
POD = {
    "metadata": {"name": "nelua-api-abc", "labels": {"app.kubernetes.io/name": "nelua-api"}},
    "spec": {"nodeName": "node-a"},
    "status": {
        "phase": "Running",
        "startTime": "2026-10-05T14:31:00Z",
        "containerStatuses": [{"ready": True, "restartCount": 2}],
    },
}
OTHER_POD = {
    "metadata": {"name": "otro-xyz", "labels": {"app.kubernetes.io/name": "otro"}},
    "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
}
AUTOSCALER = {
    "spec": {
        "scaleTargetRef": {"kind": "Deployment", "name": "nelua-api"},
        "minReplicas": 3,
        "maxReplicas": 30,
        "metrics": [
            {"type": "Resource", "resource": {"name": "cpu", "target": {"averageUtilization": 60}}}
        ],
    },
    "status": {
        "currentReplicas": 3,
        "desiredReplicas": 5,
        "currentMetrics": [
            {"type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 85}}}
        ],
    },
}
NODE = {
    "metadata": {
        "name": "node-a",
        "creationTimestamp": "2026-10-05T13:00:00Z",
        "labels": {
            "topology.kubernetes.io/zone": "us-east-2a",
            "node.kubernetes.io/instance-type": "c7g.large",
            "kubernetes.io/arch": "arm64",
            "karpenter.sh/capacity-type": "spot",
            "karpenter.sh/nodepool": "graviton",
        },
    },
    "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "nodeInfo": {"kubeletVersion": "v1.34.1"},
    },
}


def replicaset(name, revision, tag, created, replicas=0, ready=0, history=None, owner="nelua-api"):
    annotations = {"deployment.kubernetes.io/revision": str(revision)}
    if history:
        annotations["deployment.kubernetes.io/revision-history"] = history
    return {
        "metadata": {
            "name": name,
            "namespace": "nelua-api",
            "creationTimestamp": created,
            "annotations": annotations,
            "ownerReferences": [{"kind": "Deployment", "name": owner}] if owner else [],
        },
        "spec": {
            "replicas": replicas,
            "template": {"spec": {"containers": [{"image": f"{IMAGE}:{tag}"}]}},
        },
        "status": {"readyReplicas": ready},
    }


# Historia: se desplegó abc123 (rev 3), luego bad999 (rev 4) y se hizo rollback:
# Kubernetes reactivó el ReplicaSet de abc123 con la revisión 5.
REPLICASETS = [
    replicaset("nelua-api-aaa", 5, "abc123", "2026-10-04T10:00:00Z", 3, 3, history="3"),
    replicaset("nelua-api-bbb", 4, "bad999", "2026-10-05T14:00:00Z"),
    replicaset("suelto", 1, "x", "2026-10-01T00:00:00Z", owner=None),
]


def warning_event(name, reason, minutes_ago, message="detalle", count=1):
    seen = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "involvedObject": {"kind": "Pod", "name": name},
        "reason": reason,
        "message": message,
        "count": count,
        "lastTimestamp": seen.isoformat().replace("+00:00", "Z"),
    }


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("token-de-prueba")
    return str(path)


async def test_kubernetes_source_reads_only_the_watched_namespaces(token_file):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token-de-prueba"
        requested.append(request.url.path)
        path = request.url.path
        if path.endswith("/deployments"):
            return httpx.Response(200, json={"items": [DEPLOYMENT]})
        if path.endswith("/pods"):
            return httpx.Response(200, json={"items": [POD, OTHER_POD]})
        if path.endswith("/horizontalpodautoscalers"):
            return httpx.Response(200, json={"items": [AUTOSCALER]})
        if path.endswith("/replicasets"):
            return httpx.Response(200, json={"items": REPLICASETS})
        if path.endswith("/events"):
            assert request.url.params["fieldSelector"] == "type=Warning"
            return httpx.Response(
                200, json={"items": [warning_event("nelua-api-abc", "BackOff", 2)]}
            )
        return httpx.Response(200, json={"items": [NODE]})

    source = KubernetesSource(
        "https://kubernetes.test", token_file, "", ("nelua-api",), httpx.MockTransport(handler)
    )
    state = await source.collect()
    await source.close()

    assert requested == [
        "/apis/apps/v1/namespaces/nelua-api/deployments",
        "/api/v1/namespaces/nelua-api/pods",
        "/apis/autoscaling/v2/namespaces/nelua-api/horizontalpodautoscalers",
        "/apis/apps/v1/namespaces/nelua-api/replicasets",
        "/api/v1/namespaces/nelua-api/events",
        "/api/v1/nodes",
    ]

    deployment = state.deployments[0]
    assert deployment.status == "healthy"
    assert deployment.version == "abc123"
    assert [pod.name for pod in deployment.pods] == ["nelua-api-abc"]  # solo sus pods
    assert deployment.pods[0].restarts == 2
    assert deployment.autoscaling.desired_replicas == 5
    assert deployment.autoscaling.cpu_current_percent == 85

    node = state.nodes[0]
    assert (node.ready, node.zone, node.capacity_type) == (True, "us-east-2a", "spot")

    assert [r.revision for r in state.revisions] == [5, 4]
    assert [e.reason for e in state.events] == ["BackOff"]


def test_parse_revisions_marks_the_active_one_and_rollbacks():
    active, rolled_back = parse_revisions(REPLICASETS, [DEPLOYMENT])  # el suelto se ignora

    assert (active.revision, active.version, active.active) == (5, "abc123", True)
    assert active.reactivated is True  # volvió a activarse: fue un rollback
    assert active.replicas.ready == 3

    assert (rolled_back.revision, rolled_back.version) == (4, "bad999")
    assert (rolled_back.active, rolled_back.reactivated) == (False, False)


def test_parse_events_keeps_recent_ones_and_deduplicates():
    not_before = datetime.now(UTC) - timedelta(minutes=30)
    events = parse_events(
        [
            warning_event("pod-a", "BackOff", 20, "viejo"),
            warning_event("pod-a", "BackOff", 1, "nuevo", count=7),
            warning_event("pod-a", "Unhealthy", 5),
            warning_event("pod-b", "FailedScheduling", 90),  # fuera de la ventana
        ],
        "nelua-api",
        not_before,
    )
    assert sorted((e.name, e.reason, e.message) for e in events) == [
        ("pod-a", "BackOff", "nuevo"),
        ("pod-a", "Unhealthy", "detalle"),
    ]
    assert events[0].namespace == "nelua-api"


async def test_kubernetes_source_propagates_api_errors(token_file):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"message": "forbidden"})
    )
    source = KubernetesSource("https://kubernetes.test", token_file, "", ("nelua-api",), transport)
    with pytest.raises(httpx.HTTPStatusError):
        await source.collect()
    await source.close()


@pytest.mark.parametrize(
    ("desired", "ready", "updated", "conditions", "expected"),
    [
        (0, 0, 0, [], "scaled_down"),
        (3, 3, 3, [], "healthy"),
        (3, 1, 3, [{"type": "Available", "status": "False"}], "unavailable"),
        (
            3,
            2,
            1,
            [{"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated"}],
            "progressing",
        ),
        (
            3,
            2,
            3,
            [{"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}],
            "degraded",
        ),
        (
            3,
            2,
            2,
            [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}],
            "degraded",
        ),
    ],
)
def test_deployment_status(desired, ready, updated, conditions, expected):
    assert (
        deployment_status(desired, ready, updated, {"status": {"conditions": conditions}})
        == expected
    )


@pytest.mark.parametrize(
    ("image", "version"),
    [
        ("registry.example.com:5000/team/app:1.2.3", "1.2.3"),
        ("nginx", "latest"),
        ("repo/app@sha256:abc", "sha256:abc"),
    ],
)
def test_split_image(image, version):
    assert split_image(image) == version


# ---------- AWS Budgets ----------

AWS_BUDGET = {
    "BudgetName": "mensual",
    "TimeUnit": "MONTHLY",
    "BudgetLimit": {"Amount": "50.0", "Unit": "USD"},
    "CalculatedSpend": {
        "ActualSpend": {"Amount": "42.5", "Unit": "USD"},
        "ForecastedSpend": {"Amount": "61.0", "Unit": "USD"},
    },
}


def test_parse_budget():
    budget = parse_budget(AWS_BUDGET)
    assert budget.percent_used == 85.0
    assert budget.status == "warning"
    assert budget.forecasted_spend.amount == 61.0


@pytest.mark.parametrize(
    ("percent", "forecast", "expected"),
    [
        (10, 20, "ok"),
        (80, 45, "warning"),
        (40, 55, "warning"),
        (100, 120, "exceeded"),
        (30, None, "ok"),
    ],
)
def test_budget_status(percent, forecast, expected):
    assert budget_status(percent, forecast, 50) == expected


async def test_aws_budget_source_uses_the_account_of_its_credentials():
    calls: dict = {}

    class FakeClient:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

        def describe_budgets(self, AccountId):  # noqa: N803 (nombre de la API de AWS)
            calls["account"] = AccountId
            return {"Budgets": [AWS_BUDGET]}

    class FakeSession:
        def client(self, service, **kwargs):
            calls.setdefault("services", []).append((service, kwargs.get("region_name")))
            return FakeClient()

    budgets = await AwsBudgetSource(FakeSession()).collect()
    assert calls["account"] == "123456789012"
    assert ("budgets", "us-east-1") in calls["services"]
    assert budgets[0].name == "mensual"


# ---------- API keys ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('["a", "b"]', ("a", "b")), ("a, b", ("a", "b")), (" solo-una ", ("solo-una",)), ("", ())],
)
def test_parse_keys(raw, expected):
    assert parse_keys(raw) == expected
