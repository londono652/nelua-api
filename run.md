# Cómo correr y desplegar

## 1. En local

Solo hace falta Docker.

```bash
cd api
cp .env.example .env          # define la API key local; .env no se versiona
docker compose up --build
```

La API queda en `http://localhost:8000`. Como en local no hay clúster ni cuenta de
AWS, responde con una foto de ejemplo.

```bash
KEY=cambia-esta-llave-local

curl -s localhost:8000/healthz
curl -si localhost:8000/v1/summary                                  # 401: falta la llave
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/summary
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/alerts
curl -s -H "X-API-Key: $KEY" "localhost:8000/v1/deployments?namespace=nelua-api"
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/budget
```

La documentación interactiva está en <http://localhost:8000/docs>.

### Pruebas y calidad

```bash
cd api
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest -q
```

### Configuración

La API se configura con variables de entorno. No hay valores sensibles en el código.

| Variable | Para qué | Por defecto |
|---|---|---|
| `API_KEYS` | Llaves válidas, separadas por coma (uso local) | (ninguno) |
| `API_KEYS_SECRET_ID` | Secreto de Secrets Manager con las llaves (en AWS) | (ninguno) |
| `CLUSTER_SOURCE` | `kubernetes` (real) o `sample` | `sample` |
| `BUDGET_SOURCE` | `aws` (real), `sample` o `none` | `sample` |
| `WATCH_NAMESPACES` | Namespaces que se exponen | `nelua-api` |
| `CLUSTER_REFRESH_SECONDS` | Cada cuánto se consulta el clúster | `15` |
| `BUDGET_REFRESH_SECONDS` | Cada cuánto se consulta AWS Budgets | `900` |
| `ALERT_POD_RESTARTS` | Reinicios de un pod a partir de los cuales hay alerta | `3` |
| `ALERT_MIN_ZONES` | Zonas mínimas con nodos listos | `2` |

## 2. Despliegue en AWS

Se necesita una cuenta de AWS, un dominio, Terraform 1.11 o superior, AWS CLI,
`gh`, `kubectl` y Helm 4.

La infraestructura está repartida en tres stacks. Los separé por cada cuánto
cambian y por lo que cuestan.

| Stack | Qué crea | Cómo se aplica | Costo |
|---|---|---|---|
| `bootstrap` | Bucket del estado, confianza OIDC con GitHub y zona DNS | A mano, una vez | Centavos |
| `persistent` | Lo que comparten los ambientes: ECR, certificado y secretos de las API keys | Pipeline | Centavos |
| `platform` | Un ambiente completo: VPC, EKS, ALB, WAF, IAM de la API y alarmas | Pipeline, una vez por ambiente y con interruptor | Por hora |

El stack `platform` se aplica una vez por ambiente. El código es el mismo y cada
ambiente tiene su archivo de valores y su estado:

```
iac/stacks/platform/envs/
  staging.tfvars   staging.backend.hcl
  prod.tfvars      prod.backend.hcl
```

### Paso 1: bootstrap (una sola vez)

```bash
cd iac/stacks/bootstrap
terraform init
terraform apply
```

Este stack se aplica a mano y guarda su estado en local. Es el que crea el bucket
donde los otros dos guardan el suyo y los roles con los que el pipeline entra a
AWS, así que el pipeline no puede crearlo. Tampoco conviene que el pipeline pueda
cambiar sus propios permisos.

Las salidas traen los ARN de los dos roles para GitHub y los name servers que hay
que poner en el registrador del dominio.

### Paso 2: configurar el repositorio

```bash
R=<usuario>/nelua-api
gh variable set AWS_APP_ROLE_ARN   --repo $R --body "<gha_app_role_arn>"
gh variable set AWS_INFRA_ROLE_ARN --repo $R --body "<gha_infra_role_arn>"
gh variable set STAGING_ENABLED    --repo $R --body "false"
gh variable set PROD_ENABLED       --repo $R --body "false"
gh variable set DEPLOY_ENABLED     --repo $R --body "false"
```

Hay que crear los environments `staging`, `prod` e `infra`. Los dos últimos llevan
*Required reviewers*. En GitHub no se guarda ningún secreto.

### Paso 3: crear un ambiente

1. `gh variable set STAGING_ENABLED --body "true"`.
2. Correr el pipeline **infra**, con un push a `iac/**` o con *Run workflow*.
3. Revisar el plan y aprobar. El pipeline crea el ambiente y deja su clúster configurado.

Producción se crea igual, con `PROD_ENABLED`. El pipeline muestra su plan en cada
ejecución aunque esté apagada.

Para aplicar un ambiente a mano, sin el pipeline:

```bash
cd iac/stacks/platform
terraform init -reconfigure -backend-config=envs/staging.backend.hcl
terraform apply -var-file=envs/staging.tfvars
bash ../../scripts/apply-k8s.sh staging
```

### Paso 4: desplegar la API

1. `gh variable set DEPLOY_ENABLED --body "true"`.
2. Correr el pipeline **app**, con un push a `api/**` o con *Run workflow*.
3. Despliega en staging y verifica. Si producción está encendida, pide aprobación
   y despliega ahí la misma imagen.

Para probarla ya desplegada:

```bash
# La llave está en Secrets Manager y se lee solo cuando hace falta.
KEY=$(aws secretsmanager get-secret-value --secret-id nelua-api/staging/api-keys \
  --query SecretString --output text | jq -r '.[0]')
curl -s -H "X-API-Key: $KEY" https://api-staging.<dominio>/v1/summary
```

### Operación

| Tarea | Cómo |
|---|---|
| Revertir un despliegue | Workflow **rollback**, eligiendo el ambiente |
| Rotar la API key | Subir `api_key_version` en `iac/stacks/persistent` y aplicar. La API la recarga en 5 minutos sin redesplegar |
| Ver métricas | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` |
| Prueba de carga | `bash cicd/load-tests/run-in-cluster.sh staging`, con `load_test_mode = true` en `envs/staging.tfvars` |
| Apagar un ambiente | Workflow **ops-down**, eligiendo el ambiente, y su variable en `false` |
