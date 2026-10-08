"""Comportamiento ante fallos: almacén vacío o caído, fuentes caídas y falta de llaves."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.collector import Collector
from app.main import create_app
from app.reader import SnapshotReader, meta
from app.sources.sample import SampleClusterSource, SampleDeploySource
from app.store import MemoryStore, Snapshot

REPO = "londono652/nelua-api"


class FailingSource:
    name = "failing"

    async def collect(self):
        raise RuntimeError("fuente caída")

    async def list_deploys(self, repo, known):
        raise RuntimeError("fuente caída")


class FailingStore(MemoryStore):
    async def get_snapshots(self, names):
        raise RuntimeError("DynamoDB no responde")


def test_not_ready_until_the_store_can_be_read(settings, auth):
    with TestClient(create_app(settings, FailingStore())) as client:
        assert client.get("/healthz").status_code == 200  # el proceso vive
        assert client.get("/readyz").status_code == 503  # pero no recibe tráfico


def test_empty_store_answers_503_with_the_error_format(settings, auth, wait_ready):
    with TestClient(create_app(settings, MemoryStore())) as client:
        wait_ready(client)  # leyó el almacén, aunque todavía no haya fotos
        for path in ("/v1/deployments", f"/v1/repos/{REPO}/deploys"):
            response = client.get(path, headers=auth)
            assert response.status_code == 503
            assert response.headers["content-type"] == "application/problem+json"


def test_not_ready_without_api_keys(settings, store):
    with TestClient(create_app(replace(settings, api_keys=()), store)) as client:
        assert client.get("/readyz").status_code == 503


async def test_reader_keeps_last_snapshots_when_the_store_fails(store):
    reader = SnapshotReader(store, (REPO,), 1)
    await reader.refresh()
    before = reader.get("cluster")

    # Se simula una caída de DynamoDB: las fotos en memoria siguen sirviendo.
    store.get_snapshots = FailingStore().get_snapshots
    try:
        await reader.refresh()
    except RuntimeError:
        pass
    assert reader.get("cluster") is before
    assert reader.deploys(REPO) is not None


async def test_collector_keeps_last_snapshot_when_a_source_fails(settings):
    memory = MemoryStore()
    await Collector(settings, memory, SampleClusterSource(), SampleDeploySource()).collect_cluster()
    first = memory.snapshots["cluster"]["collected_at"]

    failing = Collector(settings, memory, FailingSource(), FailingSource())
    for collect in (failing.collect_cluster, failing.collect_deploys):
        try:
            await collect()
        except RuntimeError:
            pass
    assert memory.snapshots["cluster"]["collected_at"] == first


def test_old_snapshot_is_marked_as_stale():
    def snapshot(age_seconds: int) -> Snapshot:
        collected = datetime.now(UTC) - timedelta(seconds=age_seconds)
        return Snapshot(
            payload=[], source="sample", refresh_seconds=15, collected_at=collected.isoformat()
        )

    assert meta(snapshot(10))["stale"] is False
    assert meta(snapshot(60))["stale"] is True  # más de 3 ciclos sin renovarse
    assert meta(None) == {"collected_at": None, "stale": True, "source": "none", "sync": None}


class OneRepoFails:
    """GitHub responde para un repo y falla para el otro."""

    name = "github"

    async def list_deploys(self, repo, known):
        if repo == "otro/repo":
            request = httpx.Request("GET", f"https://api.github.com/repos/{repo}/deployments")
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )
        return await SampleDeploySource().list_deploys(repo, known)


class RecordedMetrics:
    def __init__(self):
        self.records = []

    async def record(self, source, ok):
        self.records.append((source, ok))


async def test_a_failing_repo_does_not_block_the_others(settings):
    settings = replace(settings, github_repos=("otro/repo", REPO))
    memory, metrics = MemoryStore(), RecordedMetrics()
    collector = Collector(settings, memory, SampleClusterSource(), OneRepoFails(), metrics)

    await collector.sync_deploys()
    await collector.sync_deploys()

    assert f"deploys#{REPO}" in memory.snapshots  # el repo sano se actualizó
    failed = collector.sync_status["deploys#otro/repo"]
    assert failed["consecutive_failures"] == 2
    assert failed["last_success_at"] is None
    assert failed["error"] == "api.github.com respondió HTTP 404 en /repos/otro/repo/deployments"
    assert collector.sync_status[f"deploys#{REPO}"]["error"] is None
    # La métrica de GitHub cuenta como falla si algún repo falló: eso dispara la alarma.
    assert metrics.records == [("github", False), ("github", False)]


async def test_cluster_failure_is_reported_and_recovers(settings):
    memory, metrics = MemoryStore(), RecordedMetrics()
    failing = Collector(settings, memory, FailingSource(), SampleDeploySource(), metrics)
    await failing.sync_cluster()
    assert failing.sync_status["cluster"]["error"] == "RuntimeError: fuente caída"

    healthy = Collector(settings, memory, SampleClusterSource(), SampleDeploySource(), metrics)
    healthy.sync_status = failing.sync_status
    await healthy.sync_cluster()
    status = healthy.sync_status["cluster"]
    assert (status["consecutive_failures"], status["error"]) == (0, None)
    assert status["last_success_at"] == status["last_attempt_at"]
    assert metrics.records == [("cluster", False), ("cluster", True)]


def test_api_explains_why_data_is_not_fresh(settings, auth, wait_ready):
    async def prepare():
        memory = MemoryStore()
        await Collector(
            settings, memory, SampleClusterSource(), SampleDeploySource()
        ).sync_cluster()
        await Collector(settings, memory, FailingSource(), SampleDeploySource()).sync_cluster()
        return memory

    import asyncio

    memory = asyncio.run(prepare())
    with TestClient(create_app(settings, memory)) as client:
        wait_ready(client)
        meta_ = client.get("/v1/deployments", headers=auth).json()["meta"]
        assert meta_["sync"]["error"] == "RuntimeError: fuente caída"
        assert meta_["sync"]["consecutive_failures"] == 1
        assert meta_["sync"]["last_success_at"] is None  # el estado no conoce el éxito anterior
        assert meta_["collected_at"] is not None  # pero sigue sirviendo la foto anterior

        # Un repo sin ninguna recolección todavía.
        assert client.get(f"/v1/repos/{REPO}/deploys", headers=auth).status_code == 503


async def test_a_restarted_collector_remembers_the_last_success(settings):
    memory = MemoryStore()
    first = Collector(settings, memory, SampleClusterSource(), SampleDeploySource())
    await first.sync_cluster()
    success = first.sync_status["cluster"]["last_success_at"]
    first.sync_status["deploys#repo/retirado"] = {"error": "viejo"}
    await first.sync_cluster()  # guarda también la vista del repo retirado

    restarted = Collector(settings, memory, FailingSource(), SampleDeploySource())
    await restarted.restore_sync_status()
    await restarted.sync_cluster()
    status = restarted.sync_status["cluster"]
    assert status["last_success_at"] >= success
    assert status["consecutive_failures"] == 1
    assert "deploys#repo/retirado" not in restarted.sync_status


def test_jwt_mode_trusts_the_load_balancer(settings, store, wait_ready):
    # En producción el ALB valida el token de Cognito; la API no pide llave.
    jwt = replace(settings, auth_mode="jwt", api_keys=())
    with TestClient(create_app(jwt, store)) as client:
        wait_ready(client)
        assert client.get("/v1/deployments").status_code == 200
