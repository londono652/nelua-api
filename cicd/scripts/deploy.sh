#!/usr/bin/env bash
# Despliega una versión de la API en un entorno.
#   uso: bash cicd/scripts/deploy.sh <staging|prod> <tag-de-imagen>
#
# Primer nivel de rollback: si los pods nuevos no quedan listos a tiempo, Helm
# deshace el cambio por sí solo (--rollback-on-failure) y este script falla.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

ENVIRONMENT="$1"
IMAGE_TAG="$2"

connect_cluster "$ENVIRONMENT"
REPOSITORY=$(param ecr/repository-url)
SECRET_ID=$(param "secrets/api-keys-$ENVIRONMENT")

# Se anota la revisión que está sirviendo ANTES de tocar nada: es el punto al
# que vuelve el paso de rollback si la verificación posterior falla.
PREVIOUS=$(current_revision)
output previous_revision "$PREVIOUS"
echo "Revisión actual en $ENVIRONMENT: ${PREVIOUS:-ninguna (primer despliegue)}"

echo "Desplegando $REPOSITORY:$IMAGE_TAG en $ENVIRONMENT..."
helm upgrade --install "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/values-$ENVIRONMENT.yaml" \
  --set image.repository="$REPOSITORY" \
  --set image.tag="$IMAGE_TAG" \
  --set apiKeys.secretId="$SECRET_ID" \
  --set awsRegion="${AWS_REGION:-us-east-2}" \
  --wait --rollback-on-failure --timeout 5m

summary "Desplegada la versión \`$IMAGE_TAG\` en **$ENVIRONMENT** (revisión $(current_revision), anterior: ${PREVIOUS:-ninguna})."
