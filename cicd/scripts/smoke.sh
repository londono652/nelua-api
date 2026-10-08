#!/usr/bin/env bash
# Prueba de humo con carga ligera (k6) contra la URL pública del entorno.
#   uso: bash cicd/scripts/smoke.sh <staging|prod>
set -euo pipefail
source "$(dirname "$0")/lib.sh"

ENVIRONMENT="$1"
URL="https://$(param "dns/hostname-$ENVIRONMENT")"
load_credentials "$ENVIRONMENT"

k6 run -e BASE_URL="$URL" -e AUTH_HEADER="$AUTH_HEADER" "$ROOT/cicd/load-tests/smoke.js"
