"""Trazas distribuidas con OpenTelemetry y logs que llevan el mismo identificador.

Se activa solo si hay a dónde mandar las trazas (OTEL_EXPORTER_OTLP_ENDPOINT).
Sin eso, OpenTelemetry queda en modo no-op: los spans del código no cuestan nada
y en local y en las pruebas todo funciona igual.

El resto se configura con las variables estándar de OpenTelemetry, que pone el chart:
  OTEL_SERVICE_NAME            nelua-api o nelua-api-collector
  OTEL_EXPORTER_OTLP_ENDPOINT  el colector de trazas del clúster (http://...:4318)
  OTEL_TRACES_SAMPLER(_ARG)    qué fracción de las trazas se guarda
  OTEL_RESOURCE_ATTRIBUTES     ambiente y versión

Qué se ve en una traza:
  - API: la petición HTTP (excepto /healthz, /readyz y /metrics) y la relectura
    periódica de DynamoDB. Las peticiones responden desde memoria, así que su
    traza tiene un solo span: eso es lo esperado, no una falta de instrumentación.
  - Recolector: cada ciclo de sincronización con un span por vista, y dentro las
    llamadas a GitHub, a la API de Kubernetes, a DynamoDB y a CloudWatch.
"""

import logging
import os
from typing import Any

from opentelemetry import trace

from app.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"

# Rutas que no generan trazas: las llaman Kubernetes y Prometheus cada pocos segundos.
EXCLUDED_URLS = "/healthz,/readyz,/metrics"

_active = False


def enabled() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def active() -> bool:
    """True si este proceso está exportando trazas."""
    return _active


def setup_tracing(settings: Settings, service_name: str) -> bool:
    """Configura el exportador y la instrumentación de httpx y boto3.

    Se llama una vez al arrancar el proceso, antes de crear clientes.
    """
    global _active
    if _active or not enabled():
        return _active

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    attributes: dict[str, Any] = {
        "service.namespace": "nelua-api",
        "service.version": settings.app_version,
        "deployment.environment.name": settings.environment,
    }
    if not os.getenv("OTEL_SERVICE_NAME"):
        attributes["service.name"] = service_name

    # El muestreo lo decide OTEL_TRACES_SAMPLER: el SDK lo lee al crear el provider.
    provider = TracerProvider(resource=Resource.create(attributes))
    # En lotes y en otro hilo: si el colector de trazas no responde, las trazas
    # se descartan, pero la petición nunca espera por ellas.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    _active = True
    logging.getLogger("nelua.tracing").info(
        "Trazas activas: %s -> %s",
        os.getenv("OTEL_SERVICE_NAME", service_name),
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    return True


def _alb_trace_header(span: Any, scope: dict[str, Any]) -> None:
    """Guarda el X-Amzn-Trace-Id que agrega el ALB, para cruzar la traza con sus logs."""
    if span is None or not span.is_recording():
        return
    for name, value in scope.get("headers", []):
        if name == b"x-amzn-trace-id":
            span.set_attribute("aws.alb.trace_id", value.decode("latin-1"))
            return


def instrument_app(app: Any, tracer_provider: Any = None) -> None:
    """Un span por petición HTTP, sin los spans internos de envío y recepción del ASGI."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls=EXCLUDED_URLS,
        server_request_hook=_alb_trace_header,
        exclude_spans=["receive", "send"],
    )


# ---------- Logs ----------


def _with_trace_id(factory):
    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x") if context.is_valid else "-"
        return record

    return record_factory


def configure_logging() -> None:
    """Formato de log común. Cada línea lleva el trace_id del span en curso (o "-").

    Con ese identificador se pasa de un error en los logs a su traza en X-Ray, y al
    revés. Se agrega en la fábrica de registros y no en un handler, para que
    cualquier handler (también los de uvicorn o pytest) pueda usar el formato.
    """
    factory = logging.getLogRecordFactory()
    if not getattr(factory, "_nelua_trace_id", False):
        wrapped = _with_trace_id(factory)
        wrapped._nelua_trace_id = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(wrapped)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # Cada petición de httpx se registra en INFO; con consultas cada 15 s llenan el log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
