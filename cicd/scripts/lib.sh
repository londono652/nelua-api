#!/usr/bin/env bash
# Funciones compartidas por los scripts de despliegue.
#
# Todo lo que necesitan de la infraestructura lo leen de Parameter Store:
# los scripts no conocen ARNs, nombres de clúster ni dominios.

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
current_revision() {
  helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null | jq -r '.version // empty'
}

# Versión (tag de imagen) que Helm tiene registrada como desplegada.
deployed_version() {
  helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output json | jq -r '.image.tag'
}

# Lee la API key del ambiente desde Secrets Manager y la oculta en los logs.
load_api_key() {
  local secret
  secret=$(param "secrets/api-keys-$1")
  API_KEY=$(aws secretsmanager get-secret-value --secret-id "$secret" \
    --query SecretString --output text | jq -r '.[0]')
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::add-mask::$API_KEY"
  fi
  export API_KEY
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
