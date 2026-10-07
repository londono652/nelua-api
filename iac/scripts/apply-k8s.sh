#!/usr/bin/env bash
# Aplica en el clúster los manifiestos que pertenecen a la plataforma (no a la app):
# namespaces, pool de nodos, permisos de lectura de la API, el enlace entre los
# Services y el ALB, y el monitoreo.
#   uso: bash iac/scripts/apply-k8s.sh
set -euo pipefail

PROJECT="${PROJECT:-nelua-api}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

param() {
  aws ssm get-parameter --name "/$PROJECT/$1" --query Parameter.Value --output text
}

aws eks update-kubeconfig --name "$(param eks/cluster-name)" >/dev/null

kubectl apply -f "$ROOT/k8s/namespaces.yaml"
kubectl apply -f "$ROOT/k8s/nodepool.yaml"
kubectl apply -f "$ROOT/k8s/rbac.yaml"

for ENVIRONMENT in staging prod; do
  TARGET_GROUP_ARN=$(param "alb/target-group-arn-$ENVIRONMENT")
  export ENVIRONMENT TARGET_GROUP_ARN
  envsubst '${ENVIRONMENT} ${TARGET_GROUP_ARN}' < "$ROOT/k8s/targetgroupbinding.yaml" | kubectl apply -f -
done

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
