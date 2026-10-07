#!/usr/bin/env bash
# Verifica desde fuera (DNS -> WAF -> ALB -> pods) que el entorno sirve la
# versión esperada y que los endpoints de negocio responden.
#   uso: bash cicd/scripts/verify.sh <staging|prod> [versión-esperada]
#
# Si no se indica versión, se espera la que Helm tiene como desplegada.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

ENVIRONMENT="$1"
connect_cluster
EXPECTED="${2:-$(deployed_version "$ENVIRONMENT")}"
URL="https://$(param "dns/hostname-$ENVIRONMENT")"
load_api_key "$ENVIRONMENT"

echo "Verificando que $URL sirve la versión $EXPECTED..."
for attempt in $(seq 1 30); do
  version=$(curl -fsS --max-time 5 "$URL/healthz" | jq -r .version 2>/dev/null || true)
  if [ "$version" = "$EXPECTED" ]; then
    break
  fi
  echo "Intento $attempt: versión actual '${version:-sin respuesta}', esperando..."
  sleep 5
done
if [ "$version" != "$EXPECTED" ]; then
  echo "ERROR: $ENVIRONMENT no respondió con la versión $EXPECTED" >&2
  exit 1
fi

# Sin llave debe rechazar; con llave, cada endpoint debe responder 200.
status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/v1/summary")
if [ "$status" != "401" ]; then
  echo "ERROR: /v1/summary sin API key respondió $status (se esperaba 401)" >&2
  exit 1
fi

# Las métricas no deben ser visibles desde internet.
status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/metrics")
if [ "$status" != "404" ]; then
  echo "ERROR: /metrics es accesible desde fuera (respondió $status, se esperaba 404)" >&2
  exit 1
fi

for path in /v1/summary /v1/deployments /v1/alerts /v1/budget; do
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "X-API-Key: $API_KEY" "$URL$path")
  echo "  $path -> $status"
  if [ "$status" != "200" ]; then
    echo "ERROR: $path respondió $status" >&2
    exit 1
  fi
done

summary "Verificación correcta: **$ENVIRONMENT** sirve la versión \`$EXPECTED\`."
