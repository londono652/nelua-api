"""Lado de la API: mantiene en memoria las fotos que escribe el recolector.

Cada pod relee las fotos de DynamoDB cada pocos segundos. Las peticiones de los
clientes nunca tocan DynamoDB ni las fuentes: responden desde memoria.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from app.collector import BUDGET_SNAPSHOT, SYNC_SNAPSHOT, deploys_snapshot_name
from app.store import Snapshot, Store

logger = logging.getLogger("nelua.reader")
tracer = trace.get_tracer("nelua.reader")

# Una foto se considera desactualizada si lleva más de 3 ciclos sin renovarse.
STALE_AFTER_CYCLES = 3


def _utc(value: str | None) -> str | None:
    return value.replace("+00:00", "Z") if value else None


def meta(
    snapshot: Snapshot | None, sync: dict[str, Any] | None = None, source: str = "none"
) -> dict[str, Any]:
    """Qué tan reciente es el dato y, si la sincronización está fallando, por qué."""
    sync_info = (
        {
            "last_attempt_at": _utc(sync.get("last_attempt_at")),
            "last_success_at": _utc(sync.get("last_success_at")),
            "consecutive_failures": sync.get("consecutive_failures", 0),
            "error": sync.get("error"),
        }
        if sync
        else None
    )
    if snapshot is None:
        return {"collected_at": None, "stale": True, "source": source, "sync": sync_info}
    age = (datetime.now(UTC) - snapshot.collected_at).total_seconds()
    return {
        "collected_at": _utc(snapshot["collected_at"]),
        "stale": age > snapshot.refresh_seconds * STALE_AFTER_CYCLES,
        "source": snapshot.source,
        "sync": sync_info,
    }


class SnapshotReader:
    def __init__(self, store: Store, repos: tuple[str, ...], refresh_seconds: int) -> None:
        self._store = store
        self.repos = repos
        self._refresh_seconds = refresh_seconds
        self.views = [
            "cluster",
            BUDGET_SNAPSHOT,
            *(deploys_snapshot_name(repo) for repo in repos),
        ]
        self.names = [*self.views, SYNC_SNAPSHOT]
        self._snapshots: dict[str, Snapshot] = {}
        self.loaded = False

    def get(self, name: str) -> Snapshot | None:
        return self._snapshots.get(name)

    def deploys(self, repo: str) -> Snapshot | None:
        return self._snapshots.get(deploys_snapshot_name(repo))

    def sync(self, name: str) -> dict[str, Any] | None:
        """Cómo le fue al recolector la última vez que intentó actualizar esta vista."""
        status = self._snapshots.get(SYNC_SNAPSHOT)
        return status.payload.get(name) if status else None

    def meta(self, name: str) -> dict[str, Any]:
        return meta(self._snapshots.get(name), self.sync(name))

    async def refresh(self) -> None:
        with tracer.start_as_current_span("refresh snapshots") as span:
            snapshots = await self._store.get_snapshots(self.names)
            span.set_attribute("nelua.snapshots", len(snapshots))
        # Si una foto no vino (todavía no existe) se conserva la anterior.
        self._snapshots = {**self._snapshots, **snapshots}
        self.loaded = True

    async def refresh_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("No se pudo leer el almacén; se conservan las fotos anteriores")
            await asyncio.sleep(self._refresh_seconds)
