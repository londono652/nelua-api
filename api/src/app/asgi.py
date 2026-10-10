"""Punto de entrada para uvicorn: `uvicorn app.asgi:app`."""

from app.config import load_settings
from app.main import create_app
from app.tracing import setup_tracing

settings = load_settings()
# Antes de crear la app: los clientes de boto3 y httpx que se crean después ya
# salen instrumentados.
setup_tracing(settings, "nelua-api")
app = create_app(settings)
