import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.collector import Collector
from app.config import Settings, load_settings
from app.main import create_app
from app.sources.sample import SampleBudgetSource, SampleClusterSource, SampleDeploySource
from app.store import MemoryStore

API_KEY = "test-key"
REPO = "londono652/nelua-api"


@pytest.fixture
def settings() -> Settings:
    return replace(
        load_settings(),
        api_keys=(API_KEY,),
        environment="test",
        github_repos=(REPO,),
        snapshot_refresh_seconds=1,
    )


def wait_until_ready(client: TestClient, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/readyz").status_code == 200:
            return
        time.sleep(0.02)
    raise AssertionError("La API no quedó lista a tiempo")


@pytest.fixture
def wait_ready():
    """Entrega la función de espera a las pruebas que arman su propia app."""
    return wait_until_ready


@pytest.fixture
async def store(settings) -> MemoryStore:
    """Un almacén con lo que dejaría el recolector después de una vuelta."""
    memory = MemoryStore()
    collector = Collector(
        settings,
        memory,
        SampleClusterSource(),
        SampleDeploySource(),
        budget_source=SampleBudgetSource(),
    )
    await collector.sync_cluster()
    await collector.sync_deploys()
    await collector.sync_budget()
    return memory


@pytest.fixture
def client(settings, store):
    app = create_app(settings, store)
    # El bloque "with" ejecuta el arranque de la app, que empieza a leer el almacén.
    with TestClient(app) as test_client:
        wait_until_ready(test_client)
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}
