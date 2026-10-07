"""Contrato HTTP de la API: endpoints, autenticación y formato de errores."""

import pytest

PROBLEM = "application/problem+json"
V1_PATHS = [
    "/v1/summary",
    "/v1/deployments",
    "/v1/alerts",
    "/v1/budget",
]


def assert_problem(response, status: int) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert set(body) == {"type", "title", "status", "detail", "instance"}
    assert body["status"] == status
    return body


# ---------- Autenticación ----------


@pytest.mark.parametrize("path", V1_PATHS)
def test_business_endpoints_require_api_key(client, path):
    body = assert_problem(client.get(path), 401)
    assert body["type"] == "urn:nelua:problem:unauthorized"


def test_invalid_api_key_is_rejected(client):
    assert_problem(client.get("/v1/summary", headers={"X-API-Key": "otra"}), 401)


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_operational_endpoints_do_not_require_api_key(client, path):
    assert client.get(path).status_code == 200


# ---------- Endpoints de negocio ----------


def test_summary(client, auth):
    response = client.get("/v1/summary", headers=auth)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["environment"] == "test"
    nodes = data["nodes"]
    assert (nodes["total"], nodes["ready"]) == (3, 3)
    assert nodes["by_zone"] == {"us-east-2a": 1, "us-east-2b": 1, "us-east-2c": 1}
    assert nodes["by_capacity_type"] == {"spot": 2, "on-demand": 1}
    assert [n["name"] for n in nodes["items"]] == [
        "sample-node-a",
        "sample-node-b",
        "sample-node-c",
    ]
    assert nodes["items"][0]["architecture"] == "arm64"
    assert data["deployments"] == {"total": 2, "by_status": {"healthy": 1, "progressing": 1}}
    assert data["budget"] == {"status": "ok", "budgets": 1, "highest_percent_used": 36.8}
    assert data["alerts"] == {"total": 2, "by_severity": {"warning": 2}}


def test_every_response_says_how_fresh_the_data_is(client, auth):
    meta = client.get("/v1/deployments", headers=auth).json()["meta"]
    assert meta["source"] == "sample"
    assert meta["stale"] is False
    assert meta["collected_at"].endswith("Z")


def test_list_deployments(client, auth):
    items = client.get("/v1/deployments", headers=auth).json()["data"]
    assert [(d["namespace"], d["name"]) for d in items] == [
        ("prod", "nelua-api"),
        ("staging", "nelua-api"),
    ]
    assert items[0]["version"] == "sample-1a2b3c4"
    assert len(items[0]["pods"]) == 3
    assert items[0]["autoscaling"]["max_replicas"] == 30
    assert items[0]["replicas"] == {"desired": 3, "ready": 3, "updated": 3, "available": 3}


def test_list_deployments_filters(client, auth):
    by_namespace = client.get("/v1/deployments", params={"namespace": "staging"}, headers=auth)
    assert [d["namespace"] for d in by_namespace.json()["data"]] == ["staging"]

    by_status = client.get("/v1/deployments", params={"status": "healthy"}, headers=auth)
    assert [d["status"] for d in by_status.json()["data"]] == ["healthy"]

    one = client.get(
        "/v1/deployments", params={"namespace": "prod", "name": "nelua-api"}, headers=auth
    )
    assert len(one.json()["data"]) == 1

    unknown = client.get("/v1/deployments", params={"name": "no-existe"}, headers=auth)
    assert unknown.json()["data"] == []


def test_list_deployments_rejects_unknown_status(client, auth):
    body = assert_problem(
        client.get("/v1/deployments", params={"status": "roto"}, headers=auth), 422
    )
    assert "status" in body["detail"]


def test_deployment_history_shows_the_rollback(client, auth):
    prod = client.get("/v1/deployments", params={"namespace": "prod"}, headers=auth)
    history = prod.json()["data"][0]["history"]
    assert [r["revision"] for r in history] == [5, 4, 2]  # la más reciente primero
    assert [r["active"] for r in history] == [True, False, False]
    # La activa es una versión anterior que volvió: así se ve un rollback.
    assert history[0]["reactivated"] is True
    assert history[0]["version"] == "sample-1a2b3c4"


# ---------- Alertas ----------


def test_list_alerts(client, auth):
    body = client.get("/v1/alerts", headers=auth).json()
    assert {a["code"] for a in body["data"]} == {"pod_restarting", "kubernetes_warning"}
    alert = next(a for a in body["data"] if a["code"] == "pod_restarting")
    assert alert["severity"] == "warning"
    assert alert["resource"] == {
        "kind": "Pod",
        "name": "nelua-api-7d9f8b6c5-klmno",
        "namespace": "prod",
    }
    assert "4 veces" in alert["message"]
    assert set(body["meta"]) == {"cluster", "budget"}


def test_list_alerts_filters(client, auth):
    staging = client.get("/v1/alerts", params={"namespace": "staging"}, headers=auth)
    assert [a["code"] for a in staging.json()["data"]] == ["kubernetes_warning"]

    critical = client.get("/v1/alerts", params={"severity": "critical"}, headers=auth)
    assert critical.json()["data"] == []

    assert_problem(client.get("/v1/alerts", params={"severity": "grave"}, headers=auth), 422)


def test_get_budget(client, auth):
    budget = client.get("/v1/budget", headers=auth).json()["data"][0]
    assert budget["limit"] == {"amount": 50.0, "unit": "USD"}
    assert budget["status"] == "ok"


# ---------- Errores y operación ----------


def test_unknown_route_uses_the_same_error_format(client):
    assert_problem(client.get("/no-existe"), 404)


def test_wrong_method_uses_the_same_error_format(client, auth):
    assert_problem(client.post("/v1/summary", headers=auth), 405)


def test_healthz_reports_version(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_metrics_do_not_explode_with_unknown_paths(client, auth):
    client.get("/v1/deployments", params={"namespace": "prod"}, headers=auth)
    client.get("/v1/no-existe/123", headers=auth)
    body = client.get("/metrics").text
    assert 'path="/v1/deployments"' in body
    # Las rutas desconocidas se agrupan: no crean una serie por cada URL.
    assert 'path="unmatched"' in body
    assert "/v1/no-existe/123" not in body
    assert 'path="/healthz"' not in body
    assert "collector_last_success_timestamp_seconds" in body
    assert 'active_alerts{severity="warning"} 2.0' in body
