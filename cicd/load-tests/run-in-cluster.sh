#!/usr/bin/env bash
# Lanza la prueba de carga de 10.000 RPS desde dentro del clúster.
#   uso: bash cicd/load-tests/run-in-cluster.sh
#
# Requisitos:
#   - kubectl y aws conectados con permisos de administrador.
#   - El WAF en modo prueba de carga (variable load_test_mode = true en el stack
#     platform), para que no bloquee la IP de salida del clúster.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/../scripts/lib.sh"

connect_cluster
load_api_key prod

kubectl create namespace loadtest --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap k6-script --namespace loadtest \
  --from-file=load.js="$DIR/load.js" --dry-run=client -o yaml | kubectl apply -f -

# La llave se entrega a los generadores como Secret, no como texto en el Job.
kubectl create secret generic k6-api-key --namespace loadtest \
  --from-literal=api-key="$API_KEY" --dry-run=client -o yaml | kubectl apply -f -

# Un Job no se puede modificar: se borra el anterior antes de lanzar otro.
kubectl delete job k6-load --namespace loadtest --ignore-not-found --wait
kubectl apply -f "$DIR/k6-job.yaml"

echo
echo "Prueba lanzada. Para seguirla:"
echo "  kubectl get pods -n loadtest -w"
echo "  kubectl get hpa,pods -n prod -w"
echo "  kubectl logs -n loadtest -l app=k6-load -f --max-log-requests 4"
