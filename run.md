# Cómo correr y desplegar

## 1. En local

Solo hace falta Docker.

```bash
cd api
cp .env.example .env          # define la API key local; .env no se versiona
docker compose up --build
```

Levanta tres contenedores, igual que en el clúster pero sin AWS:

- `dynamodb`: DynamoDB Local, en memoria.
- `collector`: el recolector. Crea la tabla si no existe y escribe las fotos.
- `api`: la API, en `http://localhost:8000`.

Por defecto los datos son de ejemplo. Los despliegues de ejemplo se fechan
relativos al momento en que arrancas, para que siempre caigan dentro de las
ventanas de 7 y 30 días.

```bash
KEY=cambia-esta-llave-local
R=londono652/nelua-api

curl -s localhost:8000/healthz
curl -si localhost:8000/v1/repos                                            # 401: falta la llave
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/repos
curl -s -H "X-API-Key: $KEY" "localhost:8000/v1/repos/$R/deploys?environment=prod&limit=5"
curl -s -H "X-API-Key: $KEY" "localhost:8000/v1/repos/$R/deploys/stats?days=7"
curl -s -H "X-API-Key: $KEY" "localhost:8000/v1/deployments?namespace=nelua-api"
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/budget
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/repos/otro/repo/deploys          # 404: no monitoreado
```

### Con los despliegues reales de GitHub

En `.env`:

```bash
GITHUB_SOURCE=github
GITHUB_REPOS=londono652/nelua-api
GITHUB_TOKEN=          # opcional para repos públicos
```

Sin token funciona con repos públicos, pero GitHub permite solo 60 peticiones por
hora por IP. Alcanza para probar un rato. El recolector hace una por repo cada
minuto, más una por cada despliegue que siga en curso.

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

La API y el recolector se configuran con variables de entorno. No hay valores
sensibles en el código.

| Variable | Quién la usa | Para qué | Por defecto |
|---|---|---|---|
| `TABLE_NAME` | los dos | Tabla de DynamoDB | `nelua-api-local` |
| `GITHUB_REPOS` | los dos | Repositorios monitoreados (`owner/repo`, separados por coma) | `londono652/nelua-api` |
| `DYNAMODB_ENDPOINT` | los dos | Solo para DynamoDB Local | (ninguno) |
| `API_KEYS` | API | Llaves válidas, separadas por coma (uso local) | (ninguno) |
| `API_KEYS_SECRET_ID` | API | Secreto de Secrets Manager con las llaves (en AWS) | (ninguno) |
| `SNAPSHOT_REFRESH_SECONDS` | API | Cada cuánto relee las fotos de DynamoDB | `5` |
| `CLUSTER_SOURCE` | recolector | `kubernetes` (real) o `sample` | `sample` |
| `GITHUB_SOURCE` | recolector | `github` (real) o `sample` | `sample` |
| `BUDGET_SOURCE` | recolector | `aws` (AWS Budgets), `sample` o `none` | `sample` |
| `BUDGET_REFRESH_SECONDS` | recolector | Cada cuánto consulta AWS Budgets | `900` |
| `GITHUB_TOKEN` | recolector | Token de GitHub (uso local) | (ninguno) |
| `GITHUB_TOKEN_SECRET_ID` | recolector | Secreto con el token de GitHub (en AWS) | (ninguno) |
| `WATCH_NAMESPACES` | recolector | Namespaces que se exponen | `nelua-api` |
| `DEPLOY_ENVIRONMENTS` | recolector | Ambientes de GitHub que cuentan como despliegues | `staging,prod` |
| `CLUSTER_REFRESH_SECONDS` | recolector | Cada cuánto consulta el clúster | `15` |
| `GITHUB_REFRESH_SECONDS` | recolector | Cada cuánto consulta GitHub | `60` |
| `CREATE_TABLE` | recolector | Crea la tabla si no existe (solo local) | `false` |
| `METRICS_NAMESPACE` | recolector | Namespace de CloudWatch para la métrica `SyncSuccess` (vacío: no publica) | (ninguno) |

## 2. Despliegue en AWS

Se necesita una cuenta de AWS, un dominio, Terraform 1.11 o superior, AWS CLI,
`gh`, `kubectl` y Helm 4.

La infraestructura está repartida en tres stacks. Los separé por cada cuánto
cambian y por lo que cuestan.

| Stack | Qué crea | Cómo se aplica | Costo |
|---|---|---|---|
| `bootstrap` | Bucket del estado, confianza OIDC con GitHub y zona DNS | A mano, una vez | Centavos |
| `persistent` | Lo que comparten los ambientes: ECR, certificado, secretos de las API keys y el del token de GitHub, y el presupuesto mensual de la cuenta | Pipeline | Centavos |
| `platform` | Un ambiente completo: VPC, EKS, ALB, WAF, DynamoDB, IAM de la API y del recolector, y alarmas | Pipeline, una vez por ambiente y con interruptor | Por hora |

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

### Paso 4: guardar el token de GitHub

El stack `persistent` crea el secreto `nelua-api/github-token` vacío. El token no
pasa por Terraform, el repositorio ni el pipeline: lo guardo yo directo en Secrets
Manager.

1. En GitHub: *Settings → Developer settings → Fine-grained tokens → Generate new
   token*. Repository access: solo los repos monitoreados. Para repos públicos no
   hace falta ningún permiso extra; para privados, *Deployments: Read-only*.
2. Guardarlo sin que quede en el historial de la terminal:

```bash
read -rs GH_TOKEN   # pegar el token y Enter; no se muestra
aws secretsmanager put-secret-value --secret-id nelua-api/github-token \
  --secret-string "$GH_TOKEN"
unset GH_TOKEN
```

Si el recolector ya estaba corriendo, lee el token solo al arrancar:
`kubectl rollout restart deployment/nelua-api-collector -n nelua-api`.

Sin token también funciona con repos públicos, con el límite de 60 peticiones por
hora para todo el clúster. Si se agota, la alarma `nelua-api-<ambiente>-sync-github`
lo avisa y `meta.sync.error` muestra el HTTP 403 de GitHub.

### Paso 5: desplegar la API

1. `gh variable set DEPLOY_ENABLED --body "true"`.
2. Correr el pipeline **app**, con un push a `api/**` o con *Run workflow*.
3. Despliega en staging y verifica. Si producción está encendida, pide aprobación
   y despliega ahí la misma imagen.

Para probarla ya desplegada:

```bash
# La llave está en Secrets Manager y se lee solo cuando hace falta.
KEY=$(aws secretsmanager get-secret-value --secret-id nelua-api/staging/api-keys \
  --query SecretString --output text | jq -r '.[0]')
curl -s -H "X-API-Key: $KEY" https://api-staging.<dominio>/v1/repos/londono652/nelua-api/deploys/stats
```

### Llamar a producción (Cognito)

Producción no usa API key: cada consumidor tiene un cliente de Cognito y pide un
token de una hora. Los datos públicos (URL del token, scope e ids de los clientes)
salen en `terraform output oauth` del stack `platform` de producción.

```bash
POOL=<user_pool_id>   # el que aparece en el issuer de terraform output oauth
CLIENT=<client_id del consumidor>
SECRET=$(aws cognito-idp describe-user-pool-client --user-pool-id "$POOL" \
  --client-id "$CLIENT" --query UserPoolClient.ClientSecret --output text)
TOKEN=$(curl -s -X POST "<token_url>" -u "$CLIENT:$SECRET" \
  -d grant_type=client_credentials -d scope=nelua-api/read | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" https://api.<dominio>/v1/repos
```

Para agregar un consumidor, se suma su nombre a `api_consumers` en
`envs/prod.tfvars` y se aplica. Para revocarlo, se quita de la lista.

### Operación

| Tarea | Cómo |
|---|---|
| Revertir un despliegue | Workflow **rollback**, eligiendo el ambiente |
| Ver por qué los datos están viejos | `meta.sync.error` en cualquier respuesta, o `kubectl logs deployment/nelua-api-collector -n nelua-api` |
| Monitorear otro repo | Agregarlo a `githubRepos` en `api/chart/values-<ambiente>.yaml` y desplegar |
| Monitorear otro namespace | Agregarlo a `watchNamespaces` en el chart y su RoleBinding en `iac/k8s/rbac.yaml` |
| Rotar el token de GitHub | `put-secret-value` con el nuevo y reiniciar el recolector |
| Rotar la API key | Subir `api_key_version` en `iac/stacks/persistent` y aplicar. La API la recarga en 5 minutos sin redesplegar |
| Ver métricas | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` |
| Prueba de carga | `bash cicd/load-tests/run-in-cluster.sh staging`, con `load_test_mode = true` en `envs/staging.tfvars` |
| Apagar un ambiente | Workflow **ops-down**, eligiendo el ambiente, y su variable en `false` |
