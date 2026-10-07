"""Manejo de errores con un único formato para toda la API.

Todas las respuestas de error siguen RFC 9457 ("Problem Details for HTTP
APIs"): mismo cuerpo y mismo Content-Type, sea un 401, un 404, un 422 o un 500.
Quien consume la API maneja los errores de una sola forma.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("nelua.errors")

PROBLEM_MEDIA_TYPE = "application/problem+json"

TITLES = {
    400: ("bad-request", "Petición inválida"),
    401: ("unauthorized", "No autenticado"),
    403: ("forbidden", "Sin permiso"),
    404: ("not-found", "Recurso no encontrado"),
    405: ("method-not-allowed", "Método no permitido"),
    422: ("validation-error", "Parámetros inválidos"),
    500: ("internal-error", "Error interno"),
    503: ("unavailable", "Servicio no disponible"),
}


class ApiError(Exception):
    """Error de negocio que la API traduce a una respuesta Problem Details."""

    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.detail = detail
        self.headers = headers


def problem(
    request: Request, status: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    slug, title = TITLES.get(status, ("error", "Error"))
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
        content={
            "type": f"urn:nelua:problem:{slug}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem(request, exc.status, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Error en la petición"
        return problem(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(
            f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
            for error in exc.errors()
        )
        return problem(request, 422, fields)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # El detalle real va al log; al cliente nunca se le exponen datos internos.
        logger.exception("Error no controlado en %s", request.url.path)
        return problem(request, 500, "Ocurrió un error inesperado")
