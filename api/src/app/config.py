"""Configuración, leída siempre de variables de entorno.

No hay valores sensibles en el código: las API keys y el token de GitHub llegan
por variable de entorno (local) o se leen de AWS Secrets Manager (en el clúster).
"""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_version: str
    environment: str
    aws_region: str

    # ---------- Almacén (DynamoDB) ----------
    table_name: str
    # Solo en local: apunta a DynamoDB Local y crea la tabla si no existe.
    dynamodb_endpoint: str
    create_table: bool

    # ---------- API ----------
    # Cómo se autentican los clientes:
    #   "api_key"  la API exige el encabezado X-API-Key (local y staging).
    #   "jwt"      el ALB ya validó un token de Cognito antes de dejar pasar la
    #              petición (producción); la API no vuelve a pedir llave.
    auth_mode: str
    # Cada cuánto los pods de la API releen la foto desde DynamoDB.
    snapshot_refresh_seconds: int
    api_keys: tuple[str, ...]
    api_keys_secret_id: str
    api_keys_refresh_seconds: int

    # ---------- Recolector ----------
    # De dónde salen los datos: "kubernetes"/"github" (reales) o "sample" (ejemplo).
    cluster_source: str
    github_source: str
    watch_namespaces: tuple[str, ...]
    cluster_refresh_seconds: int
    github_refresh_seconds: int
    # Repositorios que se monitorean ("owner/repo"). La API solo responde por estos.
    github_repos: tuple[str, ...]
    github_token: str
    github_token_secret_id: str
    # Ambientes de GitHub que cuentan como despliegues de la aplicación.
    deploy_environments: tuple[str, ...]
    heartbeat_file: str
    # Namespace de CloudWatch donde se publica si cada sincronización salió bien.
    # Vacío: no se publica (local y pruebas).
    metrics_namespace: str

    k8s_api_url: str
    k8s_token_file: str
    k8s_ca_file: str


def load_settings() -> Settings:
    env = os.getenv
    return Settings(
        app_version=env("APP_VERSION", "dev"),
        environment=env("ENVIRONMENT", "local"),
        aws_region=env("AWS_REGION", "us-east-2"),
        table_name=env("TABLE_NAME", "nelua-api-local"),
        dynamodb_endpoint=env("DYNAMODB_ENDPOINT", ""),
        create_table=env("CREATE_TABLE", "false").lower() == "true",
        auth_mode=env("AUTH_MODE", "api_key"),
        snapshot_refresh_seconds=int(env("SNAPSHOT_REFRESH_SECONDS", "5")),
        api_keys=_csv(env("API_KEYS", "")),
        api_keys_secret_id=env("API_KEYS_SECRET_ID", ""),
        api_keys_refresh_seconds=int(env("API_KEYS_REFRESH_SECONDS", "300")),
        cluster_source=env("CLUSTER_SOURCE", "sample"),
        github_source=env("GITHUB_SOURCE", "sample"),
        watch_namespaces=_csv(env("WATCH_NAMESPACES", "nelua-api")),
        cluster_refresh_seconds=int(env("CLUSTER_REFRESH_SECONDS", "15")),
        github_refresh_seconds=int(env("GITHUB_REFRESH_SECONDS", "60")),
        github_repos=_csv(env("GITHUB_REPOS", "londono652/nelua-api")),
        github_token=env("GITHUB_TOKEN", ""),
        github_token_secret_id=env("GITHUB_TOKEN_SECRET_ID", ""),
        deploy_environments=_csv(env("DEPLOY_ENVIRONMENTS", "staging,prod")),
        metrics_namespace=env("METRICS_NAMESPACE", ""),
        heartbeat_file=env("HEARTBEAT_FILE", "/tmp/collector-heartbeat"),  # noqa: S108
        k8s_api_url=env("K8S_API_URL", "https://kubernetes.default.svc"),
        k8s_token_file=env("K8S_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
        k8s_ca_file=env("K8S_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
    )
