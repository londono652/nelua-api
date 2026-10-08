"""Fuente de GitHub: traducción de estados y cuidado del rate limit."""

import httpx
import pytest

from app.sources.github import GitHubSource, build_deploy

REPO = "londono652/nelua-api"


def deployment(id_, environment="staging", created="2026-10-08T10:00:00Z"):
    return {
        "id": id_,
        "environment": environment,
        "sha": f"sha{id_}",
        "ref": "main",
        "creator": {"login": "londono652"},
        "created_at": created,
    }


def status(state, created="2026-10-08T10:03:20Z"):
    return {"state": state, "created_at": created, "log_url": "https://github.com/run/1"}


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("success", "success"),
        ("inactive", "success"),
        ("failure", "failure"),
        ("error", "failure"),
        ("in_progress", "in_progress"),
        ("queued", "in_progress"),
        ("algo-nuevo", "in_progress"),
    ],
)
def test_build_deploy_maps_github_states(state, expected):
    assert build_deploy(deployment(1), status(state)).status == expected


def test_build_deploy_computes_duration_only_when_finished():
    done = build_deploy(deployment(1), status("success"))
    assert done.duration_seconds == 200
    assert done.run_url == "https://github.com/run/1"
    assert done.creator == "londono652"

    running = build_deploy(deployment(1), status("in_progress"))
    assert (running.finished_at, running.duration_seconds) == (None, None)

    no_status = build_deploy(deployment(1), None)
    assert (no_status.status, no_status.github_state) == ("in_progress", "pending")


async def test_does_not_ask_again_for_finished_deploys(caplog):
    requested: list[str] = []
    deployments = [deployment(1), deployment(2), deployment(3, environment="preview")]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token-de-prueba"
        requested.append(request.url.path)
        headers = {"x-ratelimit-remaining": "42"}
        if request.url.path.endswith("/deployments"):
            return httpx.Response(200, json=deployments, headers=headers)
        return httpx.Response(200, json=[status("success")], headers=headers)

    source = GitHubSource(
        "token-de-prueba", ("staging", "prod"), transport=httpx.MockTransport(handler)
    )
    first = await source.list_deploys(REPO, {})
    # Segunda vuelta: el 1 y el 2 ya terminaron, no se vuelve a preguntar por ellos.
    second = await source.list_deploys(REPO, {d.id: d for d in first})
    await source.close()

    assert [d.id for d in first] == [1, 2]  # "preview" no es un ambiente monitoreado
    assert second == first
    assert requested == [
        f"/repos/{REPO}/deployments",
        f"/repos/{REPO}/deployments/1/statuses",
        f"/repos/{REPO}/deployments/2/statuses",
        f"/repos/{REPO}/deployments",
    ]
    assert "Quedan 42 peticiones" in caplog.text


async def test_github_errors_propagate():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))
    source = GitHubSource("malo", ("staging",), transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        await source.list_deploys(REPO, {})
    await source.close()


async def test_works_without_token_for_public_repos():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    source = GitHubSource("", ("staging",), transport=httpx.MockTransport(handler))
    assert await source.list_deploys(REPO, {}) == []
    await source.close()


async def test_stops_asking_for_deploys_stuck_in_progress():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[deployment(1, created="2026-01-01T00:00:00Z")])
        return httpx.Response(200, json=[status("in_progress")])

    source = GitHubSource("t", ("staging",), transport=httpx.MockTransport(handler))
    first = await source.list_deploys(REPO, {})
    assert first[0].status == "in_progress"
    # Lleva más de 24 horas "en curso" (un job cancelado): no se vuelve a preguntar.
    assert await source.list_deploys(REPO, {1: first[0]}) == first
    await source.close()
    assert requested.count(f"/repos/{REPO}/deployments/1/statuses") == 1
