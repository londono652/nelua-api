"""Traducción de las respuestas de Kubernetes al modelo de la API."""

import httpx
import pytest

from app.auth import parse_keys
from app.sources.kubernetes import (
    KubernetesSource,
    deployment_status,
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
        return httpx.Response(404)

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
    ]

    deployment = state.deployments[0]
    assert deployment.status == "healthy"
    assert deployment.version == "abc123"
    assert [pod.name for pod in deployment.pods] == ["nelua-api-abc"]  # solo sus pods
    assert deployment.pods[0].restarts == 2
    assert deployment.autoscaling.desired_replicas == 5
    assert deployment.autoscaling.cpu_current_percent == 85

    assert [r.revision for r in state.revisions] == [5, 4]


def test_parse_revisions_marks_the_active_one_and_rollbacks():
    active, rolled_back = parse_revisions(REPLICASETS, [DEPLOYMENT])  # el suelto se ignora

    assert (active.revision, active.version, active.active) == (5, "abc123", True)
    assert active.reactivated is True  # volvió a activarse: fue un rollback
    assert active.replicas.ready == 3

    assert (rolled_back.revision, rolled_back.version) == (4, "bad999")
    assert (rolled_back.active, rolled_back.reactivated) == (False, False)


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


# ---------- API keys ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('["a", "b"]', ("a", "b")), ("a, b", ("a", "b")), (" solo-una ", ("solo-una",)), ("", ())],
)
def test_parse_keys(raw, expected):
    assert parse_keys(raw) == expected
