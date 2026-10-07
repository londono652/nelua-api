# Cómo correr y desplegar

## 1. En local

Requisito: Docker.

```bash
cd api
cp .env.example .env          # define la API key local; .env no se versiona
docker compose up --build
```

La API queda en `http://localhost:8000`. Usa una foto de ejemplo (no hay clúster
ni cuenta de AWS en local).

```bash
KEY=cambia-esta-llave-local

curl -s localhost:8000/healthz
curl -si localhost:8000/v1/summary                                  # 401: falta la llave
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/summary
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/alerts
curl -s -H "X-API-Key: $KEY" "localhost:8000/v1/deployments?namespace=prod"
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/budget
```

Documentación interactiva: <http://localhost:8000/docs>.

### Pruebas y calidad

```bash
cd api
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest -q
```

### Configuración

Todo se configura con variables de entorno; no hay valores sensibles en el código.

| Variable | Para qué | Por defecto |
|---|---|---|
| `API_KEYS` | Llaves válidas, separadas por coma (uso local) | — |
| `API_KEYS_SECRET_ID` | Secreto de Secrets Manager con las llaves (en AWS) | — |
| `CLUSTER_SOURCE` | `kubernetes` (real) o `sample` | `sample` |
| `BUDGET_SOURCE` | `aws` (real), `sample` o `none` | `sample` |
| `WATCH_NAMESPACES` | Namespaces que se exponen | `prod,staging` |
| `CLUSTER_REFRESH_SECONDS` | Cada cuánto se consulta el clúster | `15` |
| `BUDGET_REFRESH_SECONDS` | Cada cuánto se consulta AWS Budgets | `900` |
| `ALERT_POD_RESTARTS` | Reinicios de un pod que disparan alerta | `3` |
| `ALERT_MIN_ZONES` | Zonas mínimas con nodos listos | `2` |

## 2. Despliegue en AWS

Requisitos: una cuenta de AWS, un dominio, Terraform ≥ 1.11, AWS CLI, `gh`,
`kubectl` y Helm 4.

La infraestructura está en tres stacks, según cada cuánto cambian y cuánto cuestan:

| Stack | Qué crea | Cómo se aplica | Costo |
|---|---|---|---|
| `bootstrap` | Bucket del estado, confianza OIDC con GitHub, zona DNS | Una vez, a mano | Centavos |
| `persistent` | ECR, certificado, secretos de API keys | Pipeline | Centavos |
| `platform` | VPC, EKS, ALB, WAF, IAM de la API, alarmas | Pipeline, con interruptor | Por hora |

### Paso 1: bootstrap (una sola vez)

```bash
cd iac/stacks/bootstrap
terraform init
terraform apply
```

Usa estado local a propósito: es el stack que crea el bucket donde los demás
guardan el suyo. Sus salidas incluyen los ARN de los dos roles para GitHub y los
name servers que hay que configurar en el registrador del dominio.

### Paso 2: configurar el repositorio

```bash
R=<usuario>/nelua-api
gh variable set AWS_APP_ROLE_ARN   --repo $R --body "<gha_app_role_arn>"
gh variable set AWS_INFRA_ROLE_ARN --repo $R --body "<gha_infra_role_arn>"
gh variable set PLATFORM_ENABLED   --repo $R --body "false"
gh variable set DEPLOY_ENABLED     --repo $R --body "false"
```

Crear los environments `staging`, `prod` e `infra`; los dos últimos con
*Required reviewers*. No se guarda ningún secreto en GitHub.

### Paso 3: crear la infraestructura

1. `gh variable set PLATFORM_ENABLED --body "true"`.
2. Ejecutar el pipeline **infra** (un push a `iac/**` o *Run workflow*).
3. Revisar el plan y aprobar el *Apply*. Crea la plataforma y configura el clúster.

### Paso 4: desplegar la API

1. `gh variable set DEPLOY_ENABLED --body "true"`.
2. Ejecutar el pipeline **app** (un push a `api/**` o *Run workflow*).
3. Despliega en staging, verifica y, tras aprobación, despliega en producción.

```bash
# La llave vive en Secrets Manager; se lee solo cuando hace falta.
KEY=$(aws secretsmanager get-secret-value --secret-id nelua-api/prod/api-keys \
  --query SecretString --output text | jq -r '.[0]')
curl -s -H "X-API-Key: $KEY" https://api.<dominio>/v1/summary
```

### Operación

| Tarea | Cómo |
|---|---|
| Revertir un despliegue | Workflow **rollback** → elegir entorno |
| Rotar la API key | Subir `api_key_version` en `iac/stacks/persistent` y aplicar; la API la recarga en 5 minutos, sin redesplegar |
| Ver métricas | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` |
| Prueba de carga | `bash cicd/load-tests/run-in-cluster.sh` (con `load_test_mode = true` en el WAF) |
| Apagar lo que cuesta por hora | Workflow **ops-down** y `PLATFORM_ENABLED=false` |
