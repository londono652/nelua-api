"""Fuente real: despliegues registrados en GitHub.

Cada job del pipeline que usa un environment (staging, prod) crea un
"deployment" en GitHub y le va actualizando el estado. Esta fuente los lee con
la API REST de GitHub.

GitHub limita las peticiones por hora, así que esta fuente:
  - la usa un solo proceso (el recolector), no cada pod de la API;
  - no vuelve a preguntar por despliegues que ya terminaron y están guardados.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.models import Deploy, DeployStatus

logger = logging.getLogger("nelua.github")

API_URL = "https://api.github.com"

# Estados de GitHub y cómo se cuentan. "inactive" es un despliegue que salió
# bien y después fue reemplazado por uno más nuevo en el mismo ambiente.
STATE_MAP: dict[str, DeployStatus] = {
    "success": "success",
    "inactive": "success",
    "failure": "failure",
    "error": "failure",
    "in_progress": "in_progress",
    "queued": "in_progress",
    "pending": "in_progress",
}


# Un job cancelado puede dejar el deployment "in_progress" para siempre. Pasado
# este tiempo se deja de preguntar por él, para no gastar el límite cada minuto.
ABANDONED_AFTER = timedelta(hours=24)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def build_deploy(deployment: dict[str, Any], status: dict[str, Any] | None) -> Deploy:
    """Une un deployment de GitHub con su último estado."""
    state = status["state"] if status else "pending"
    mapped = STATE_MAP.get(state, "in_progress")
    created = _time(deployment["created_at"])
    finished = _time(status.get("created_at")) if status and mapped != "in_progress" else None
    duration = int((finished - created).total_seconds()) if finished and created else None
    return Deploy(
        id=deployment["id"],
        environment=deployment["environment"],
        status=mapped,
        github_state=state,
        sha=deployment["sha"],
        ref=deployment.get("ref", ""),
        creator=(deployment.get("creator") or {}).get("login"),
        created_at=created,
        finished_at=finished,
        duration_seconds=duration,
        run_url=(status or {}).get("log_url") or (status or {}).get("target_url") or None,
    )


class GitHubSource:
    name = "github"

    def __init__(
        self,
        token: str,
        environments: tuple[str, ...],
        per_page: int = 50,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._environments = set(environments)
        self._per_page = per_page
        self._client = httpx.AsyncClient(
            base_url=API_URL,
            timeout=10,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                # Sin token funciona solo con repos públicos y con un límite de
                # 60 peticiones por hora por IP (todo el clúster sale por la
                # misma IP del NAT). Con token son 5.000 por hora.
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )

    async def _get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and int(remaining) < 100:
            logger.warning("Quedan %s peticiones a GitHub en esta hora", remaining)
        return response.json()

    async def list_deploys(self, repo: str, known: dict[int, Deploy]) -> list[Deploy]:
        deployments = await self._get(f"/repos/{repo}/deployments", per_page=self._per_page)
        abandoned_before = datetime.now(UTC) - ABANDONED_AFTER
        deploys = []
        for deployment in deployments:
            if deployment["environment"] not in self._environments:
                continue
            previous = known.get(deployment["id"])
            if previous and (previous.final or previous.created_at < abandoned_before):
                deploys.append(previous)
                continue
            # GitHub devuelve los estados del más reciente al más antiguo.
            statuses = await self._get(
                f"/repos/{repo}/deployments/{deployment['id']}/statuses", per_page=1
            )
            deploys.append(build_deploy(deployment, statuses[0] if statuses else None))
        return deploys

    async def close(self) -> None:
        await self._client.aclose()
