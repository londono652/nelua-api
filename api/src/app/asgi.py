"""Punto de entrada para uvicorn: `uvicorn app.asgi:app`."""

from app.main import create_app

app = create_app()
