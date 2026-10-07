# syntax=docker/dockerfile:1

# ---------- Etapa 1: instala las dependencias en un entorno virtual ----------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------- Etapa 2: imagen final, solo con lo necesario para ejecutar ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Aplica los parches de seguridad del sistema operativo publicados después
# de la imagen base, y crea un usuario sin privilegios: el contenedor nunca
# corre como root.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src/ .
# Garantiza que el usuario sin privilegios pueda leer el código, sin importar
# los permisos con los que venían los archivos en la máquina que construye.
RUN chmod -R a+rX /app

# La versión se inyecta al construir (el pipeline usa el SHA del commit)
# y la API la expone en /healthz.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

USER 10001
EXPOSE 8000

# Un solo proceso por contenedor: se escala agregando pods, no procesos.
# Sin access log: a 10.000 RPS sería costoso; las métricas van por /metrics.
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
