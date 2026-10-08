#!/usr/bin/env bash
# Devuelve un entorno a una revisión anterior de Helm y comprueba que quedó sano.
#   uso: bash cicd/scripts/rollback.sh <staging|prod> [revisión]
#
# Sin revisión, vuelve a la inmediatamente anterior. Lo usan:
#   - el paso "Rollback automático" del pipeline, cuando falla la verificación
#     posterior al despliegue;
#   - el workflow manual "rollback", para revertir a mano en cualquier momento.
#
# No reconstruye nada: las imágenes son inmutables y siguen en el registro, así
# que volver es apuntar de nuevo a una versión que ya funcionó.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

ENVIRONMENT="$1"
REVISION="${2:-}"

connect_cluster "$ENVIRONMENT"
FROM=$(current_revision)
FAILED_VERSION=$(deployed_version)

echo "Historial de $ENVIRONMENT antes del rollback:"
helm history "$RELEASE" --namespace "$NAMESPACE" --max 5

if [ -z "$REVISION" ] && [ "${FROM:-1}" -le 1 ]; then
  summary "No hay una revisión anterior en **$ENVIRONMENT** a la cual volver (era el primer despliegue)."
  exit 1
fi

# Sin número, Helm vuelve a la revisión anterior.
helm rollback "$RELEASE" ${REVISION:+"$REVISION"} --namespace "$NAMESPACE" --wait --timeout 5m

RESTORED_VERSION=$(deployed_version)
summary "### Rollback en $ENVIRONMENT"
summary "Se retiró la versión \`$FAILED_VERSION\` (revisión $FROM) y se restauró \`$RESTORED_VERSION\` (revisión ${REVISION:-anterior})."

# El rollback no se da por bueno hasta comprobar que el entorno responde.
bash "$(dirname "$0")/verify.sh" "$ENVIRONMENT" "$RESTORED_VERSION"
