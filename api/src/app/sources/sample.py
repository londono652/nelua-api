"""Fuentes de ejemplo para desarrollo local y pruebas.

Devuelven datos fijos guardados en data/sample.json. NO son datos reales:
permiten levantar todo con `docker compose up` sin clúster, sin cuenta de AWS
y sin token de GitHub. En el clúster siempre se usan las fuentes reales.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import ClusterState, Deploy
from app.sources.github import STATE_MAP

SAMPLE_FILE = Path(__file__).parent.parent / "data" / "sample.json"


def _load() -> dict:
    return json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))


class SampleClusterSource:
    name = "sample"

    async def collect(self) -> ClusterState:
        return ClusterState(**_load()["cluster"])


def sample_deploy(item: dict, repo: str, now: datetime) -> Deploy:
    """Arma un despliegue de ejemplo con fechas relativas a `now`.

    Las fechas no están fijas en el archivo para que los datos de ejemplo
    caigan siempre dentro de las ventanas de 7 y 30 días.
    """
    created = (now - timedelta(hours=item["hours_ago"])).replace(minute=0, second=0, microsecond=0)
    status = STATE_MAP[item["github_state"]]
    duration = item["duration_seconds"] if status != "in_progress" else None
    return Deploy(
        id=item["id"],
        environment=item["environment"],
        status=status,
        github_state=item["github_state"],
        sha=item["sha"],
        ref=item["ref"],
        creator=item["creator"],
        created_at=created,
        finished_at=created + timedelta(seconds=duration) if duration else None,
        duration_seconds=duration,
        run_url=f"https://github.com/{repo}/actions",
    )


class SampleDeploySource:
    name = "sample"

    async def list_deploys(self, repo: str, known: dict[int, Deploy]) -> list[Deploy]:
        now = datetime.now(UTC)
        # Como GitHub, un despliegue ya conocido conserva su fecha original.
        return [
            known.get(item["id"]) or sample_deploy(item, repo, now)
            for item in _load()["deploys"].get(repo, [])
        ]
