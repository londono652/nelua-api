#!/usr/bin/env bash
# Funciones compartidas por los scripts de despliegue.
#
# Todo lo que necesitan de la infraestructura lo leen de Parameter Store:
# los scripts no conocen ARNs, nombres de clúster ni dominios.

# Si un comando falla, dice cuál y en qué línea (con set -e el script se detendría
# sin explicación).
trap 'echo "ERROR: falló \"$BASH_COMMAND\" (${BASH_SOURCE[0]}:${LINENO})" >&2' ERR

PROJECT="${PROJECT:-nelua-api}"
RELEASE="nelua-api"
# El namespace es el mismo en todos los ambientes: cada uno tiene su clúster.
NAMESPACE="nelua-api"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHART="$ROOT/api/chart"

param() {
  aws ssm get-parameter --name "/$PROJECT/$1" --query Parameter.Value --output text
}

# Apunta kubectl y helm al clúster del ambiente indicado.
connect_cluster() {
  aws eks update-kubeconfig --name "$(param "$1/eks/cluster-name")" >/dev/null
}

# Revisión de Helm que está desplegada ahora (vacío si aún no hay ninguna).
# En el primer despliegue el release no existe y helm falla: eso no es un error.
current_revision() {
  { helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null || true; } |
    jq -r '.version // empty'
}

# Versión (tag de imagen) que Helm tiene registrada como desplegada.
deployed_version() {
  { helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output json 2>/dev/null || true; } |
    jq -r '.image.tag // empty'
}

# Cómo se autentican los clientes en el ambiente: "api_key" o "jwt" (Cognito).
auth_mode() {
  param "$1/auth/mode" 2>/dev/null || echo api_key
}

mask() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::add-mask::$1"
  fi
}

# Deja en AUTH_HEADER el encabezado con el que se llama a la API del ambiente:
#   api_key  la llave, leída de Secrets Manager.
#   jwt      un token de Cognito (client credentials) del cliente de verificación,
#            cuyas credenciales también están en Secrets Manager.
# Ni la llave ni el token quedan en los logs.
load_credentials() {
  local secret
  if [ "$(auth_mode "$1")" = "jwt" ]; then
    secret=$(aws secretsmanager get-secret-value --secret-id "$PROJECT/$1/oauth-client-verify" \
      --query SecretString --output text)
    local token
    token=$(curl -fsS --max-time 10 -X POST "$(jq -r .token_url <<<"$secret")" \
      -u "$(jq -r .client_id <<<"$secret"):$(jq -r .client_secret <<<"$secret")" \
      -d grant_type=client_credentials -d "scope=$(jq -r .scope <<<"$secret")" | jq -r .access_token)
    mask "$token"
    AUTH_HEADER="Authorization: Bearer $token"
  else
    secret=$(param "secrets/api-keys-$1")
    local key
    key=$(aws secretsmanager get-secret-value --secret-id "$secret" \
      --query SecretString --output text | jq -r '.[0]')
    mask "$key"
    AUTH_HEADER="X-API-Key: $key"
  fi
  export AUTH_HEADER
}

# Deja un valor disponible para los pasos siguientes del pipeline.
output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

summary() {
  echo "$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}
