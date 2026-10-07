"""Fuente de ejemplo para desarrollo local y pruebas.

Devuelve una foto fija, guardada en data/sample.json. NO son datos reales:
permite levantar la API con `docker compose up` sin un clúster ni una cuenta
de AWS. En el clúster siempre se usan las fuentes reales.
"""

import json
from pathlib import Path

from app.models import Budget, ClusterState

SAMPLE_FILE = Path(__file__).parent.parent / "data" / "sample.json"


def _load() -> dict:
    return json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))


class SampleClusterSource:
    name = "sample"

    async def collect(self) -> ClusterState:
        return ClusterState(**_load()["cluster"])


class SampleBudgetSource:
    name = "sample"

    async def collect(self) -> list[Budget]:
        return [Budget(**item) for item in _load()["budgets"]]
