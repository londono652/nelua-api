"""Cálculos sobre el historial de despliegues. Funciones puras: sin red ni estado."""

from datetime import UTC, datetime, timedelta

from app.models import Deploy, DeployStats

# Ventanas de tiempo que se precalculan y que la API acepta en ?days=.
WINDOWS = (7, 30)
LATEST_LIMIT = 100


def latest(deploys: list[Deploy], limit: int = LATEST_LIMIT) -> list[Deploy]:
    return sorted(deploys, key=lambda d: (d.created_at, d.id), reverse=True)[:limit]


def stats(
    deploys: list[Deploy], environment: str, window_days: int, now: datetime | None = None
) -> DeployStats:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    selected = [
        d for d in deploys if d.created_at >= since and environment in ("all", d.environment)
    ]
    succeeded = [d for d in selected if d.status == "success"]
    failed = [d for d in selected if d.status == "failure"]
    finished = len(succeeded) + len(failed)
    durations = [d.duration_seconds for d in selected if d.duration_seconds is not None]
    return DeployStats(
        environment=environment,
        window_days=window_days,
        total=len(selected),
        succeeded=len(succeeded),
        failed=len(failed),
        in_progress=len(selected) - finished,
        success_rate=round(100 * len(succeeded) / finished, 1) if finished else None,
        change_failure_rate=round(100 * len(failed) / finished, 1) if finished else None,
        deploys_per_day=round(len(selected) / window_days, 2),
        avg_duration_seconds=round(sum(durations) / len(durations)) if durations else None,
        last_success_at=max((d.created_at for d in succeeded), default=None),
        last_failure_at=max((d.created_at for d in failed), default=None),
    )


def all_stats(
    deploys: list[Deploy], environments: tuple[str, ...], now: datetime | None = None
) -> list[DeployStats]:
    """Métricas por ambiente y del total, para cada ventana."""
    return [
        stats(deploys, environment, window, now)
        for window in WINDOWS
        for environment in (*environments, "all")
    ]
