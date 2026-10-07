# nelua-api

API REST que le dice a un equipo de infraestructura, en una llamada, **qué está
roto, qué cambió hace poco y cuánto se lleva gastado**. Lee datos reales: el
estado del clúster de Kubernetes donde corre y los presupuestos de la cuenta de AWS.

Reto técnico *DevOps & Platform Engineering*. Todo lo que hay aquí es funcional:
la API, la infraestructura en AWS y los pipelines.

| Carpeta | Contenido |
|---|---|
| [`api/`](api) | Código (FastAPI), pruebas, `Dockerfile`, `docker-compose.yml` y chart de Helm |
| [`iac/`](iac) | Terraform (red, EKS, ALB, WAF, IAM, secretos, alarmas) y manifiestos del clúster |
| [`cicd/`](cicd) | Scripts de despliegue y rollback, pruebas de carga y [descripción de los pipelines](cicd/README.md) |
| [`.github/workflows/`](.github/workflows) | Los pipelines (GitHub Actions) |
| [`run.md`](run.md) | Cómo correrlo en local y cómo desplegarlo |
| [`docs/decisiones.md`](docs/decisiones.md) | Decisiones de diseño, alternativas y trade-offs |
| [`prompts.md`](prompts.md) | Cómo se usó IA en el reto |

## Probarlo en un minuto

```bash
cd api
cp .env.example .env
docker compose up --build
```

```bash
curl -s -H "X-API-Key: cambia-esta-llave-local" localhost:8000/v1/summary
```

En local no hay clúster ni cuenta de AWS, así que la API sirve una foto de
ejemplo. Desplegada, usa las fuentes reales. Más detalle en [`run.md`](run.md).

## La API

| Endpoint | Responde a |
|---|---|
| `GET /v1/summary` | ¿Cómo está todo? Nodos, servicios por estado, presupuesto y conteo de alertas |
| `GET /v1/alerts` | ¿Hay algo roto ahora? Problemas activos, los más graves primero |
| `GET /v1/deployments` | ¿Qué hay desplegado y qué cambió? Réplicas, versión, pods, autoescalado e historial de despliegues |
| `GET /v1/budget` | ¿Cuánto llevamos gastado? Límite, gasto, pronóstico y estado |
| `GET /healthz`, `/readyz`, `/metrics` | Operación: liveness, readiness y métricas para Prometheus |

Los endpoints `/v1` exigen el encabezado `X-API-Key`. Los errores siguen un único
formato (RFC 9457, `application/problem+json`). Cada respuesta dice qué tan fresco
es el dato (`meta.collected_at`, `meta.stale`). La documentación interactiva está
en `/docs`.

## La arquitectura

![Arquitectura de producción en AWS](docs/arquitectura.png)

El diagrama muestra el diseño de producción. Lo desplegado para el reto reduce
algunas cosas por costo (por ejemplo, un NAT Gateway en vez de tres); las
diferencias están en [`docs/decisiones.md`](docs/decisiones.md#producción-y-demo).
El archivo editable es [`docs/arquitectura.drawio`](docs/arquitectura.drawio).

- **Exposición segura:** solo el puerto 443, certificado de ACM, WAF con límite por
  IP y reglas administradas; los pods no tienen IP pública.
- **Alta disponibilidad:** tres zonas, mínimo tres réplicas repartidas entre ellas,
  despliegues sin caída.
- **Escala a 10.000 RPS:** la API no tiene estado y responde desde memoria; escalan
  los pods (HPA) y los nodos (EKS Auto Mode).
- **Sin secretos en el código ni en GitHub:** OIDC para los pipelines, Pod Identity
  para la API, Secrets Manager para las llaves.

El detalle y el porqué de cada elección están en [`docs/decisiones.md`](docs/decisiones.md).

## CI/CD

Dos pipelines independientes, cada uno con etapas definidas:

- **Aplicación:** build y test → calidad y seguridad → build de imagen → push al
  registry → deploy a staging → deploy a producción (con aprobación).
- **Infraestructura:** validación → seguridad → plan → apply (con aprobación) →
  configuración del clúster.

El **rollback** tiene tres niveles: Helm durante el despliegue, un paso automático
del pipeline si falla la verificación posterior, y un workflow manual. Ver
[`cicd/README.md`](cicd/README.md).
