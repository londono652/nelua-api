"""Trazas: qué spans genera cada componente y cómo se relacionan con los logs."""

import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import tracing
from app.collector import Collector
from app.main import create_app
from app.sources.sample import SampleClusterSource, SampleDeploySource
from app.store import MemoryStore

REPO = "londono652/nelua-api"


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


_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
# El provider global solo se puede fijar una vez por proceso.
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans():
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def test_each_sync_cycle_is_one_trace_with_a_span_per_view(settings, spans):
    settings = replace(settings, github_repos=("otro/repo", REPO))
    collector = Collector(settings, MemoryStore(), SampleClusterSource(), OneRepoFails())

    await collector.sync_deploys()

    [cycle] = by_name(spans, "sync github")
    [failed] = by_name(spans, "collect deploys#otro/repo")
    [ok] = by_name(spans, f"collect deploys#{REPO}")

    # Un ciclo es la raíz de su traza y cada repo es un hijo.
    assert cycle.parent is None
    assert {failed.parent.span_id, ok.parent.span_id} == {cycle.context.span_id}
    assert {failed.context.trace_id, ok.context.trace_id} == {cycle.context.trace_id}

    # El repo que falló queda marcado con el error y la excepción; el ciclo también.
    assert failed.status.status_code is StatusCode.ERROR
    assert "HTTP 404" in failed.status.description
    assert [e.name for e in failed.events] == ["exception"]
    assert ok.status.status_code is StatusCode.UNSET
    assert cycle.status.status_code is StatusCode.ERROR
    assert cycle.attributes["nelua.sync.ok"] is False


async def test_cycles_do_not_nest_into_each_other(settings, spans):
    collector = Collector(settings, MemoryStore(), SampleClusterSource(), OneRepoFails())

    with trace.get_tracer("test").start_as_current_span("algo que ya estaba abierto"):
        await collector.sync_cluster()

    [cycle] = by_name(spans, "sync cluster")
    assert cycle.parent is None
    assert cycle.attributes["nelua.sync.ok"] is True


def test_requests_get_a_span_and_probes_do_not(settings, store, auth, spans, wait_ready):
    app = create_app(settings, store)
    tracing.instrument_app(app, tracer_provider=_provider)
    with TestClient(app) as client:
        wait_ready(client)
        spans.clear()
        client.get("/healthz")
        client.get("/readyz")
        alb = "Root=1-67891233-abcdef012345678912345678"
        response = client.get("/v1/deployments", headers={**auth, "X-Amzn-Trace-Id": alb})

    assert response.status_code == 200
    server = [s for s in spans.get_finished_spans() if s.kind is trace.SpanKind.SERVER]
    assert [s.name for s in server] == ["GET /v1/deployments"]
    assert server[0].attributes["http.route"] == "/v1/deployments"
    assert server[0].attributes["aws.alb.trace_id"] == alb
    # Sin los spans internos de envío y recepción del ASGI.
    assert not [s.name for s in spans.get_finished_spans() if "http send" in s.name]


def test_log_lines_carry_the_trace_id(caplog):
    tracing.configure_logging()
    logger = logging.getLogger("nelua.test")

    with caplog.at_level(logging.INFO, logger="nelua.test"):
        logger.info("fuera de una traza")
        with trace.get_tracer("test").start_as_current_span("ciclo") as span:
            logger.info("dentro de una traza")

    outside, inside = caplog.records
    assert outside.trace_id == "-"
    assert inside.trace_id == format(span.get_span_context().trace_id, "032x")


def test_tracing_is_off_without_an_endpoint(monkeypatch, settings):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.enabled() is False
    assert tracing.setup_tracing(settings, "nelua-api") is False

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert tracing.enabled() is False


class UnreadableStore(MemoryStore):
    async def get_snapshots(self, names):
        raise RuntimeError("AccessDeniedException: dynamodb:BatchGetItem")


async def test_reading_the_last_state_at_startup_has_its_own_trace(settings, spans):
    collector = Collector(settings, UnreadableStore(), SampleClusterSource(), OneRepoFails())

    await collector.restore_sync_status()

    [span] = by_name(spans, "restore sync status")
    assert span.parent is None
    assert span.status.status_code is StatusCode.ERROR
    assert "AccessDeniedException" in span.status.description
