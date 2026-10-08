"""Contrato HTTP de la API: endpoints, autenticación y formato de errores."""

import pytest

PROBLEM = "application/problem+json"
DEPLOYS = "/v1/repos/londono652/nelua-api/deploys"
STATS = f"{DEPLOYS}/stats"
V1_PATHS = ["/v1/repos", DEPLOYS, STATS, "/v1/deployments", "/v1/budget"]


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
    assert_problem(client.get("/v1/repos", headers={"X-API-Key": "otra"}), 401)


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_operational_endpoints_do_not_require_api_key(client, path):
    assert client.get(path).status_code == 200


# ---------- Repositorios y despliegues ----------


def test_list_repos(client, auth):
    data = client.get("/v1/repos", headers=auth).json()["data"]
    assert [r["repo"] for r in data] == ["londono652/nelua-api"]
    assert data[0]["meta"]["stale"] is False


def test_latest_deploys(client, auth):
    body = client.get(DEPLOYS, headers=auth).json()
    deploys = body["data"]
    assert len(deploys) == 15
    created = [d["created_at"] for d in deploys]
    assert created == sorted(created, reverse=True)  # el más reciente primero
    assert deploys[0]["status"] == "in_progress"
    assert deploys[0]["finished_at"] is None
    assert {d["status"] for d in deploys} == {"success", "failure", "in_progress"}
    assert body["meta"]["source"] == "sample"


def test_latest_deploys_filters(client, auth):
    prod = client.get(DEPLOYS, params={"environment": "prod"}, headers=auth).json()["data"]
    assert len(prod) == 5
    assert {d["environment"] for d in prod} == {"prod"}

    two = client.get(DEPLOYS, params={"limit": 2}, headers=auth).json()["data"]
    assert len(two) == 2

    assert_problem(client.get(DEPLOYS, params={"limit": 0}, headers=auth), 422)
    assert_problem(client.get(DEPLOYS, params={"limit": 101}, headers=auth), 422)


def test_deploy_stats(client, auth):
    data = client.get(STATS, params={"days": 7}, headers=auth).json()["data"]
    by_env = {s["environment"]: s for s in data}
    assert set(by_env) == {"staging", "prod", "all"}

    # Últimos 7 días: staging tiene 6 despliegues (1 falla, 1 en curso).
    staging = by_env["staging"]
    assert (staging["total"], staging["succeeded"], staging["failed"]) == (6, 4, 1)
    assert staging["in_progress"] == 1
    assert staging["success_rate"] == 80.0
    assert staging["change_failure_rate"] == 20.0

    prod = by_env["prod"]
    assert (prod["total"], prod["succeeded"], prod["failed"]) == (3, 2, 1)
    assert prod["success_rate"] == 66.7

    assert by_env["all"]["total"] == 9


def test_deploy_stats_default_window_and_filter(client, auth):
    data = client.get(STATS, params={"environment": "all"}, headers=auth).json()["data"]
    assert len(data) == 1
    assert (data[0]["window_days"], data[0]["total"]) == (30, 15)
    assert data[0]["deploys_per_day"] == 0.5


def test_deploy_stats_only_accepts_known_windows(client, auth):
    body = assert_problem(client.get(STATS, params={"days": 14}, headers=auth), 422)
    assert "days" in body["detail"]


@pytest.mark.parametrize(
    "path", ["/v1/repos/otro/repo/deploys", "/v1/repos/otro/repo/deploys/stats"]
)
def test_unmonitored_repo_returns_404(client, auth, path):
    body = assert_problem(client.get(path, headers=auth), 404)
    assert "/v1/repos" in body["detail"]


def test_invalid_repo_name_is_rejected(client, auth):
    assert_problem(client.get("/v1/repos/mal%20nombre/repo/deploys", headers=auth), 422)


# ---------- Deployments del clúster ----------


def test_every_response_says_how_fresh_the_data_is(client, auth):
    meta = client.get("/v1/deployments", headers=auth).json()["meta"]
    assert meta["source"] == "sample"
    assert meta["stale"] is False
    assert meta["collected_at"].endswith("Z")
    sync = meta["sync"]
    assert (sync["consecutive_failures"], sync["error"]) == (0, None)
    assert sync["last_success_at"].endswith("Z")


def test_list_deployments(client, auth):
    items = client.get("/v1/deployments", headers=auth).json()["data"]
    assert [(d["namespace"], d["name"]) for d in items] == [
        ("nelua-api", "nelua-api"),
        ("sandbox", "nelua-api"),
    ]
    assert items[0]["version"] == "sample-1a2b3c4"
    assert len(items[0]["pods"]) == 3
    assert items[0]["autoscaling"]["max_replicas"] == 30
    assert items[0]["replicas"] == {"desired": 3, "ready": 3, "updated": 3, "available": 3}


def test_list_deployments_filters(client, auth):
    by_namespace = client.get("/v1/deployments", params={"namespace": "sandbox"}, headers=auth)
    assert [d["namespace"] for d in by_namespace.json()["data"]] == ["sandbox"]

    by_status = client.get("/v1/deployments", params={"status": "healthy"}, headers=auth)
    assert [d["status"] for d in by_status.json()["data"]] == ["healthy"]

    one = client.get(
        "/v1/deployments", params={"namespace": "nelua-api", "name": "nelua-api"}, headers=auth
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
    api = client.get("/v1/deployments", params={"namespace": "nelua-api"}, headers=auth)
    history = api.json()["data"][0]["history"]
    assert [r["revision"] for r in history] == [5, 4, 2]  # la más reciente primero
    assert [r["active"] for r in history] == [True, False, False]
    # La activa es una versión anterior que volvió: así se ve un rollback.
    assert history[0]["reactivated"] is True
    assert history[0]["version"] == "sample-1a2b3c4"


# ---------- Presupuesto ----------


def test_get_budget(client, auth):
    body = client.get("/v1/budget", headers=auth).json()
    budget = body["data"][0]
    assert budget["name"] == "nelua-api-mensual"
    assert budget["limit"] == {"amount": 300.0, "unit": "USD"}
    assert (budget["percent_used"], budget["status"]) == (62.47, "warning")
    assert body["meta"]["source"] == "sample"
    assert body["meta"]["sync"]["error"] is None


# ---------- Errores y operación ----------


def test_unknown_route_uses_the_same_error_format(client):
    assert_problem(client.get("/v1/no-existe"), 404)


def test_wrong_method_uses_the_same_error_format(client, auth):
    assert_problem(client.post("/v1/deployments", headers=auth), 405)


def test_healthz_reports_version(client):
    assert client.get("/healthz").json() == {"status": "ok", "version": "dev"}


def test_metrics_use_the_route_template_not_the_url(client, auth):
    client.get(DEPLOYS, headers=auth)
    client.get("/v1/repos/otro/repo/deploys", headers=auth)
    client.get("/v1/no-existe/abc", headers=auth)
    metrics = client.get("/metrics").text
    assert 'path="/v1/repos/{owner}/{repo}/deploys"' in metrics
    assert "otro/repo" not in metrics
    assert "no-existe" not in metrics


def test_metrics_report_snapshot_age(client):
    metrics = client.get("/metrics").text
    assert 'snapshot_age_seconds{snapshot="cluster"}' in metrics
    assert 'snapshot_age_seconds{snapshot="budget"}' in metrics
    assert 'snapshot_age_seconds{snapshot="deploys#londono652/nelua-api"}' in metrics
