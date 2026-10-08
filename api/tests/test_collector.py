"""Recolector y cálculo de métricas de despliegue."""

from datetime import UTC, datetime, timedelta

from app.collector import Collector
from app.deploys import all_stats, latest, stats
from app.models import Deploy
from app.sources.sample import SampleClusterSource
from app.store import MemoryStore

REPO = "londono652/nelua-api"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def deploy(id_, hours_ago, status="success", environment="staging", duration=100) -> Deploy:
    created = NOW - timedelta(hours=hours_ago)
    final = status != "in_progress"
    return Deploy(
        id=id_,
        environment=environment,
        status=status,
        github_state=status,
        sha="abc",
        ref="main",
        created_at=created,
        finished_at=created + timedelta(seconds=duration) if final else None,
        duration_seconds=duration if final else None,
    )


def test_stats_counts_only_finished_deploys_in_the_rate():
    deploys = [
        deploy(1, 1, "in_progress"),
        deploy(2, 2, "success", duration=100),
        deploy(3, 3, "failure", duration=50),
        deploy(4, 4, "success", duration=150),
        deploy(5, 24 * 10, "failure"),  # fuera de la ventana de 7 días
        deploy(6, 5, "success", environment="prod"),
    ]
    result = stats(deploys, "staging", 7, NOW)
    assert (result.total, result.succeeded, result.failed, result.in_progress) == (4, 2, 1, 1)
    assert result.success_rate == 66.7
    assert result.change_failure_rate == 33.3
    assert result.avg_duration_seconds == 100
    assert result.last_success_at == NOW - timedelta(hours=2)
    assert result.last_failure_at == NOW - timedelta(hours=3)


def test_stats_without_finished_deploys_has_no_rate():
    result = stats([deploy(1, 1, "in_progress")], "staging", 7, NOW)
    assert result.success_rate is None
    assert result.avg_duration_seconds is None


def test_all_stats_has_every_environment_and_window():
    keys = [(s.environment, s.window_days) for s in all_stats([], ("staging", "prod"), NOW)]
    assert keys == [
        ("staging", 7), ("prod", 7), ("all", 7),
        ("staging", 30), ("prod", 30), ("all", 30),
    ]  # fmt: skip


def test_latest_orders_and_limits():
    deploys = [deploy(i, i) for i in range(1, 6)]
    assert [d.id for d in latest(deploys, 3)] == [1, 2, 3]


class ScriptedDeploySource:
    """Devuelve lo que se le indique y registra qué historial le pasó el recolector."""

    name = "scripted"

    def __init__(self, *rounds: list[Deploy]) -> None:
        self.rounds = list(rounds)
        self.known_seen: list[set[int]] = []

    async def list_deploys(self, repo, known):
        self.known_seen.append(set(known))
        return self.rounds.pop(0)


async def test_collector_keeps_history_and_only_writes_changes(settings):
    now = datetime.now(UTC)
    running = deploy(1, 0, "in_progress").model_copy(update={"created_at": now})
    finished = running.model_copy(update={"status": "success", "duration_seconds": 90})
    older = deploy(2, 0).model_copy(update={"created_at": now - timedelta(days=2)})

    memory = MemoryStore()
    written: list[list[int]] = []
    original_put = memory.put_deploys

    async def spy_put(repo, deploys):
        written.append(sorted(d.id for d in deploys))
        await original_put(repo, deploys)

    memory.put_deploys = spy_put

    # GitHub solo devuelve la última página; el historial queda en el almacén.
    source = ScriptedDeploySource([running, older], [finished], [finished])
    collector = Collector(settings, memory, SampleClusterSource(), source)
    for _ in range(3):
        await collector.collect_repo(REPO)

    assert written == [[1, 2], [1]]  # la tercera vuelta no cambió nada: no escribe
    assert source.known_seen[1] == {1, 2}  # el recolector le pasa lo que ya conoce

    payload = memory.snapshots[f"deploys#{REPO}"]["payload"]
    assert [d["id"] for d in payload["latest"]] == [1, 2]
    assert payload["latest"][0]["status"] == "success"


async def test_collector_writes_cluster_view_with_history(settings):
    memory = MemoryStore()
    collector = Collector(settings, memory, SampleClusterSource(), ScriptedDeploySource())
    await collector.collect_cluster()
    snapshot = memory.snapshots["cluster"]
    assert snapshot["source"] == "sample"
    assert snapshot["refresh_seconds"] == settings.cluster_refresh_seconds
    assert [r["revision"] for r in snapshot["payload"][0]["history"]] == [5, 4, 2]


def test_github_token_secret_without_value_starts_without_token(settings, monkeypatch):
    from dataclasses import replace

    import boto3
    from moto import mock_aws

    from app.collector import _github_token

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("secretsmanager", region_name=settings.aws_region)
        client.create_secret(Name="nelua-api/github-token")  # sin valor, como lo crea Terraform
        configured = replace(
            settings, github_token="", github_token_secret_id="nelua-api/github-token"
        )
        assert _github_token(configured) == ""

        client.put_secret_value(SecretId="nelua-api/github-token", SecretString=" ghp_x \n")
        assert _github_token(configured) == "ghp_x"


def test_cloudwatch_metrics_are_published(monkeypatch):
    import boto3
    from moto import mock_aws

    from app.collector import CloudWatchMetrics

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        metrics = CloudWatchMetrics("nelua-api", "staging", "us-east-2")
        metrics._put("github", True)
        listed = boto3.client("cloudwatch", region_name="us-east-2").list_metrics(
            Namespace="nelua-api"
        )["Metrics"]
        assert listed[0]["MetricName"] == "SyncSuccess"
        assert {d["Name"]: d["Value"] for d in listed[0]["Dimensions"]} == {
            "Environment": "staging",
            "Source": "github",
        }
