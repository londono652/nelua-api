"""Autenticación por API key.

Las llaves válidas se cargan de una variable de entorno (desarrollo local) o de
AWS Secrets Manager (en el clúster), y se recargan periódicamente: rotar una
llave no exige reiniciar los pods. Admite varias llaves a la vez, para poder
rotar sin cortar el servicio (se agrega la nueva, se migra, se retira la vieja).
"""

import asyncio
import hmac
import json
import logging

import boto3
from fastapi import Request

from app.config import Settings
from app.errors import ApiError

logger = logging.getLogger("nelua.auth")

API_KEY_HEADER = "X-API-Key"


def parse_keys(raw: str) -> tuple[str, ...]:
    """Acepta una lista JSON (["a", "b"]) o valores separados por comas."""
    raw = raw.strip()
    if raw.startswith("["):
        return tuple(str(key) for key in json.loads(raw) if key)
    return tuple(key.strip() for key in raw.split(",") if key.strip())


class ApiKeyStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._keys: tuple[str, ...] = settings.api_keys

    @property
    def loaded(self) -> bool:
        return bool(self._keys)

    def is_valid(self, candidate: str) -> bool:
        # compare_digest evita filtrar información por el tiempo de comparación.
        encoded = candidate.encode()
        return any(hmac.compare_digest(encoded, key.encode()) for key in self._keys)

    def _fetch_secret(self) -> tuple[str, ...]:
        client = boto3.session.Session().client("secretsmanager")
        value = client.get_secret_value(SecretId=self._settings.api_keys_secret_id)
        return parse_keys(value["SecretString"])

    async def refresh(self) -> None:
        """Recarga las llaves desde Secrets Manager, si hay un secreto configurado."""
        if not self._settings.api_keys_secret_id:
            return
        keys = await asyncio.to_thread(self._fetch_secret)
        if keys:
            self._keys = keys

    async def refresh_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                # Si falla la recarga se conservan las llaves anteriores.
                logger.exception("No se pudieron recargar las API keys")
            # Mientras no haya llaves se reintenta rápido; después, al ritmo normal.
            await asyncio.sleep(self._settings.api_keys_refresh_seconds if self.loaded else 5)


def require_api_key(request: Request) -> None:
    """Dependencia de FastAPI: rechaza la petición si no trae una llave válida."""
    store: ApiKeyStore = request.app.state.api_keys
    candidate = request.headers.get(API_KEY_HEADER, "")
    if not candidate or not store.is_valid(candidate):
        raise ApiError(
            401,
            f"Falta el encabezado {API_KEY_HEADER} o la llave no es válida",
            headers={"WWW-Authenticate": "ApiKey"},
        )
