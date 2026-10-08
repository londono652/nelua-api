#!/usr/bin/env bash
# Lanza la prueba de carga de 10.000 RPS desde dentro del clúster de un ambiente.
#   uso: bash cicd/load-tests/run-in-cluster.sh [staging|prod]   (por defecto, staging)
#
# En staging, primero le sube el autoescalado a los valores de producción
# (api/chart/values-loadtest.yaml), para medir la misma capacidad sin tener que
# encender producción.
#
# Requisitos:
#   - kubectl, helm y aws conectados con permisos de administrador.
#   - El WAF en modo prueba de carga (load_test_mode = true en el archivo de
#     valores del ambiente), para que no bloquee la IP de salida del clúster.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/../scripts/lib.sh"

ENVIRONMENT="${1:-staging}"
URL="https://$(param "dns/hostname-$ENVIRONMENT")"

connect_cluster "$ENVIRONMENT"
load_api_key "$ENVIRONMENT"

if [ "$ENVIRONMENT" != "prod" ]; then
  echo "Subiendo el autoescalado de $ENVIRONMENT a los valores de producción..."
  helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" \
    --reuse-values --values "$CHART/values-loadtest.yaml" --wait --timeout 5m
fi

kubectl create namespace loadtest --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap k6-script --namespace loadtest \
  --from-file=load.js="$DIR/load.js" --dry-run=client -o yaml | kubectl apply -f -

# La llave se entrega a los generadores como Secret, no como texto en el Job.
kubectl create secret generic k6-api-key --namespace loadtest \
  --from-literal=api-key="$API_KEY" --dry-run=client -o yaml | kubectl apply -f -

# Un Job no se puede modificar: se borra el anterior antes de lanzar otro.
kubectl delete job k6-load --namespace loadtest --ignore-not-found --wait
sed "s|__BASE_URL__|$URL|" "$DIR/k6-job.yaml" | kubectl apply -f -

echo
echo "Prueba lanzada contra $URL. Para seguirla:"
echo "  kubectl get pods -n loadtest -w"
echo "  kubectl get hpa,pods -n $NAMESPACE -w"
echo "  kubectl logs -n loadtest -l app=k6-load -f --max-log-requests 4"
if [ "$ENVIRONMENT" != "prod" ]; then
  echo
  echo "Al terminar, devuelve $ENVIRONMENT a su tamaño normal:"
  echo "  helm upgrade $RELEASE $CHART -n $NAMESPACE --reuse-values --values $CHART/values-$ENVIRONMENT.yaml --wait"
fi
