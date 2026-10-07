"""Configuración de la aplicación, leída siempre de variables de entorno.

No hay valores sensibles en el código: las API keys llegan por una variable de
entorno (local) o se leen de AWS Secrets Manager (en el clúster).
"""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_version: str
    environment: str

    # De dónde salen los datos: "kubernetes" (real) o "sample" (desarrollo local).
    cluster_source: str
    # "aws" (real), "sample" (desarrollo local) o "none" (sin presupuesto).
    budget_source: str

    watch_namespaces: tuple[str, ...]
    cluster_refresh_seconds: int
    budget_refresh_seconds: int

    # Umbrales de las alertas.
    alert_pod_restarts: int
    alert_min_zones: int
    events_window_minutes: int

    # Autenticación: lista de llaves válidas o el secreto donde están guardadas.
    api_keys: tuple[str, ...]
    api_keys_secret_id: str
    api_keys_refresh_seconds: int

    k8s_api_url: str
    k8s_token_file: str
    k8s_ca_file: str


def load_settings() -> Settings:
    env = os.getenv
    return Settings(
        app_version=env("APP_VERSION", "dev"),
        environment=env("ENVIRONMENT", "local"),
        cluster_source=env("CLUSTER_SOURCE", "sample"),
        budget_source=env("BUDGET_SOURCE", "sample"),
        watch_namespaces=_csv(env("WATCH_NAMESPACES", "prod,staging")),
        cluster_refresh_seconds=int(env("CLUSTER_REFRESH_SECONDS", "15")),
        budget_refresh_seconds=int(env("BUDGET_REFRESH_SECONDS", "900")),
        alert_pod_restarts=int(env("ALERT_POD_RESTARTS", "3")),
        alert_min_zones=int(env("ALERT_MIN_ZONES", "2")),
        events_window_minutes=int(env("EVENTS_WINDOW_MINUTES", "30")),
        api_keys=_csv(env("API_KEYS", "")),
        api_keys_secret_id=env("API_KEYS_SECRET_ID", ""),
        api_keys_refresh_seconds=int(env("API_KEYS_REFRESH_SECONDS", "300")),
        k8s_api_url=env("K8S_API_URL", "https://kubernetes.default.svc"),
        k8s_token_file=env("K8S_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
        k8s_ca_file=env("K8S_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
    )
