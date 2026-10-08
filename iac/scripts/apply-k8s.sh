#!/usr/bin/env bash
# Deja configurado el clúster de un ambiente con lo que pertenece a la plataforma
# (no a la app): namespace, pool de nodos, permisos de lectura del recolector, el
# enlace entre el Service y el ALB, y el monitoreo.
#   uso: bash iac/scripts/apply-k8s.sh <staging|prod>
#
# Los manifiestos son los mismos para todos los ambientes; lo único que cambia
# es a qué clúster se aplican y el target group del balanceador.
set -euo pipefail

ENVIRONMENT="${1:?Indica el ambiente: staging o prod}"
PROJECT="${PROJECT:-nelua-api}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

param() {
  aws ssm get-parameter --name "/$PROJECT/$ENVIRONMENT/$1" --query Parameter.Value --output text
}

aws eks update-kubeconfig --name "$(param eks/cluster-name)" >/dev/null

kubectl apply -f "$ROOT/k8s/namespaces.yaml"
kubectl apply -f "$ROOT/k8s/nodepool.yaml"
kubectl apply -f "$ROOT/k8s/rbac.yaml"

TARGET_GROUP_ARN=$(param alb/target-group-arn)
export TARGET_GROUP_ARN
envsubst '${TARGET_GROUP_ARN}' < "$ROOT/k8s/targetgroupbinding.yaml" | kubectl apply -f -

# ---------- Observabilidad: Prometheus + Grafana ----------
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

# La contraseña de Grafana se genera una sola vez y vive en un Secret.
if ! kubectl get secret grafana-admin --namespace monitoring >/dev/null 2>&1; then
  kubectl create secret generic grafana-admin --namespace monitoring \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="$(openssl rand -hex 16)"
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update prometheus-community >/dev/null
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values "$ROOT/k8s/monitoring/values.yaml" \
  --wait --timeout 10m

kubectl apply -f "$ROOT/k8s/monitoring/servicemonitor.yaml"
kubectl apply -f "$ROOT/k8s/monitoring/dashboard.yaml"

kubectl get nodepools
kubectl get targetgroupbindings -A
kubectl get pods --namespace monitoring
