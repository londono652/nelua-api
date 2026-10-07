"""Comportamiento ante fallos: arranque sin datos, fuentes caídas y falta de llaves."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.snapshot import Collector, Section, Snapshot
from app.sources.sample import SampleBudgetSource, SampleClusterSource


class FailingSource:
    name = "failing"

    async def collect(self):
        raise RuntimeError("fuente caída")


def test_not_ready_until_first_collection(settings, auth):
    app = create_app(settings, FailingSource(), None)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200  # el proceso vive
        assert client.get("/readyz").status_code == 503  # pero no recibe tráfico
        response = client.get("/v1/summary", headers=auth)
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/problem+json"


def test_not_ready_without_api_keys(settings):
    app = create_app(replace(settings, api_keys=()), SampleClusterSource(), None)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503


def test_budget_failure_does_not_break_the_api(settings, auth, wait_ready):
    app = create_app(settings, SampleClusterSource(), FailingSource())
    with TestClient(app) as client:
        wait_ready(client)
        response = client.get("/v1/budget", headers=auth)
        assert response.status_code == 200
        assert response.json()["data"] == []
        assert response.json()["meta"] == {"collected_at": None, "stale": True, "source": "failing"}
        assert client.get("/v1/summary", headers=auth).json()["data"]["budget"]["status"] is None


async def test_keeps_last_snapshot_when_a_source_fails():
    snapshot = Snapshot("test", Section("sample", 15), Section("none", 900))
    await Collector(snapshot, SampleClusterSource(), None).collect_cluster()
    first = snapshot.cluster.collected_at

    # La siguiente recolección falla: la foto anterior sigue disponible.
    failing = Collector(snapshot, FailingSource(), None)
    try:
        await failing.collect_cluster()
    except RuntimeError:
        pass
    assert snapshot.ready
    assert snapshot.cluster.collected_at == first
    assert len(snapshot.deployments) == 2


async def test_old_snapshot_is_marked_as_stale():
    snapshot = Snapshot("test", Section("sample", 15), Section("none", 900))
    await Collector(snapshot, SampleClusterSource(), SampleBudgetSource()).collect_cluster()
    assert snapshot.cluster.meta()["stale"] is False

    snapshot.cluster.collected_at = datetime.now(UTC) - timedelta(seconds=60)
    assert snapshot.cluster.meta()["stale"] is True
