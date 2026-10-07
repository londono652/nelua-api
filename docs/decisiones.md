# Decisiones de diseño

Contexto que guió todo: **una sola persona** opera esto, con presupuesto razonable
y sin querer sorpresas a las 3 de la mañana. Ante cada elección la pregunta fue
cuál opción deja menos cosas que vigilar.

## 1. La API

### Qué expone y por qué

El enunciado deja el dominio libre. Se eligió responder las preguntas de quien
está de guardia, con datos reales y no inventados:

| Pregunta | Endpoint | De dónde sale el dato |
|---|---|---|
| ¿Cómo está todo? | `/v1/summary` | Agregado de lo demás |
| ¿Hay algo roto ahora? | `/v1/alerts` | Reglas sobre el estado del clúster y del presupuesto, más eventos `Warning` de Kubernetes |
| ¿Qué hay desplegado y qué cambió? | `/v1/deployments` | API de Kubernetes: deployments, pods, autoescaladores y ReplicaSets (historial) |
| ¿Cuánto llevamos gastado? | `/v1/budget` | AWS Budgets |

Con eso se cubren los cuatro ejemplos del enunciado: estado de servicios, eventos
de deploy, alertas y, vía presupuesto, costo.

### Lenguaje y framework: Python con FastAPI

- Validación de entrada y documentación OpenAPI salen de los mismos tipos: menos
  código que mantener y un contrato que no se desactualiza.
- Es asíncrono, que es lo que necesita un servicio que solo hace E/S.
- Python es el lenguaje habitual de un equipo de infraestructura: quien opere la
  API puede leerla y cambiarla.

Lo que se sacrifica es rendimiento bruto por proceso frente a Go o Rust. Se
compensa con el diseño (ver "foto en memoria") y con escalado horizontal.

### Base de datos: ninguna, a propósito

La fuente de verdad ya existe: el clúster sabe qué está desplegado y qué
revisiones hubo, y AWS sabe cuánto se ha gastado. Copiar eso a una base de datos
propia agregaría un componente con estado que respaldar, migrar y vigilar, para
guardar datos que pueden quedar desactualizados respecto al original.

El costo de esta decisión: el historial de despliegues llega hasta donde lo guarda
Kubernetes (las últimas 10 revisiones por servicio), y las alertas no tienen
historia. Si se necesitara un historial largo, el siguiente paso sería una tabla
de DynamoDB alimentada por el pipeline; no antes de necesitarla.

### La foto en memoria (la decisión que sostiene los 10.000 RPS)

Las peticiones **nunca** consultan a Kubernetes ni a AWS. Un recolector en segundo
plano refresca una foto cada 15 segundos (15 minutos para el presupuesto) y los
endpoints responden desde memoria.

- 10 o 10.000 peticiones por segundo generan la misma carga sobre las fuentes: una
  consulta por pod por ciclo. Sin esto, la API le trasladaría su tráfico al plano de
  control del clúster.
- Si una fuente falla, se sirve la última foto válida marcada como `stale`, y eso
  mismo aparece como alerta. La API no se cae porque AWS Budgets tenga un mal minuto.
- El readiness no depende de las fuentes en cada llamada: un fallo momentáneo de
  una fuente no saca los pods del balanceador.

El costo: los datos tienen hasta 15 segundos de antigüedad. Cada respuesta lo
declara en `meta.collected_at`.

### REST y no GraphQL

Son cuatro recursos de solo lectura con pocos filtros. REST da caché, códigos de
estado y herramientas (curl, k6, WAF) sin capas adicionales. GraphQL se justifica
cuando los clientes necesitan componer consultas sobre un grafo grande; aquí
agregaría complejidad y haría más difícil limitar el costo de una consulta.

### Errores

Un solo formato para todos los errores: RFC 9457 (`application/problem+json`),
con `type`, `title`, `status`, `detail` e `instance`. Aplica igual a 401, 404, 405,
422, 500 y 503. Los errores inesperados no filtran detalles internos.

### Autenticación: API key

Se eligió API key porque los consumidores son pocos y la API es interna y de solo
lectura. La llave no está en el código ni en el repositorio: vive en Secrets
Manager, hay una por entorno, la API la recarga cada 5 minutos (se rota sin
redesplegar) y se compara en tiempo constante.

Cómo llega la llave a cada parte, sin que nadie se la envíe a nadie:

1. Terraform genera una llave aleatoria por entorno y la guarda en Secrets Manager.
2. El pod la lee con su rol de IAM (Pod Identity). El chart y el pipeline solo le
   pasan el **nombre** del secreto, nunca el valor.
3. Cada consumidor la lee del mismo secreto con su propio permiso de IAM.
4. La llave viaja en el encabezado `X-API-Key`, siempre sobre HTTPS.

El control de acceso real es IAM: usa la API quien tiene permiso de leer ese
secreto, y CloudTrail registra quién lo leyó. Que la API sea pública (tiene una
dirección en internet, como pide el reto) no la hace abierta: sin llave solo
responden los endpoints de salud.

Su límite es que es un secreto compartido: no expira ni identifica a cada
consumidor. La evolución natural es JWT emitido por Cognito en flujo
máquina-a-máquina (tokens de una hora, un cliente por consumidor), validado en la
API o directamente en el ALB. No se incluyó porque agrega un componente más que
operar sin cambiar lo que el reto evalúa.

## 2. La arquitectura

![Arquitectura de producción en AWS](arquitectura.png)

| Requisito | Cómo se cumple |
|---|---|
| **Exposición pública segura** | Solo el puerto 443 (el 80 no se abre: es una API, no un sitio). TLS 1.2+ con certificado de ACM. WAF con límite por IP y reglas administradas de AWS. Pods sin IP pública; solo aceptan tráfico del ALB. API key en la aplicación. `/metrics` no se entrega desde internet (lo lee Prometheus dentro del clúster). |
| **Alta disponibilidad** | Tres zonas. Mínimo tres réplicas en producción, repartidas entre zonas. Presupuesto de interrupción (PDB). Despliegue gradual con `maxUnavailable: 0`. |
| **Escalabilidad a 10.000 RPS** | API sin estado que responde desde memoria. HPA sobre CPU (escala rápido hacia arriba, lento hacia abajo). EKS Auto Mode agrega nodos cuando hay pods que no caben. |
| **Tolerancia a fallos** | Probes de arranque, readiness y liveness. Apagado ordenado (el pod sigue atendiendo mientras el ALB lo retira). La foto en memoria aísla de fallos de las fuentes. Mezcla de Spot y On-Demand. |
| **Manejo de secretos** | Secrets Manager para las API keys; el valor no pasa por el repositorio, el pipeline ni el estado de Terraform. Sin llaves de AWS en ningún lado: OIDC para GitHub, Pod Identity para la API. |
| **Monitoreo básico** | Alarmas de CloudWatch sobre el ALB (5xx, latencia p95, pods fuera de servicio) con aviso por SNS. Prometheus y Grafana en el clúster. La propia API (`/v1/alerts`). |

### El pico de 10.000 RPS

Que sea un pico y no tráfico constante define dos cosas: cómo se escala y cuánto cuesta.

- **Un pico es un problema de velocidad.** El HPA tarda cerca de un minuto en
  reaccionar y un nodo nuevo, otro más. Mientras tanto solo atienden los pods que
  ya existen. Por eso el mínimo de réplicas no es un número arbitrario: debe cubrir
  el tráfico que puede llegar mientras se escala. Se fija con la prueba de carga
  (RPS que soporta un pod) y se deja margen: el HPA escala al 60 % de CPU, no al 100 %.
- **Si el pico es predecible** (todos los días a la misma hora), se sube el mínimo
  antes con una regla programada. **Si no lo es**, se mantiene un mínimo más alto y
  se paga ese margen. Es una decisión de costo contra riesgo.
- **La capacidad sobrante se devuelve:** el HPA baja despacio (para no oscilar) y
  los nodos vacíos se eliminan. Se paga el pico durante el pico.

**Supuesto sobre el pico.** El enunciado no precisa cuánto dura ni si es predecible.
Se asume un pico **diario, de pocas horas**. La arquitectura es la misma en
cualquier escenario; lo que cambia son tres parámetros de operación:

| Si el pico es... | Qué se ajusta | Dónde |
|---|---|---|
| Diario y predecible | Subir el mínimo de réplicas antes de la hora del pico (escalado programado) | Una regla sobre el HPA |
| Impredecible | Mantener un mínimo de réplicas más alto todo el tiempo | `api/chart/values-prod.yaml` |
| Sostenido muchas horas al día | Pasar parte de los nodos de Spot a On-Demand con descuento por compromiso, y revisar el costo del WAF por petición | Pool de nodos y estimación de costo |

**Supuesto sobre el WAF.** El límite es de 2.000 peticiones por IP cada 5 minutos:
protege contra abuso desde una sola dirección. Supone que los 10.000 RPS vienen de
muchos clientes. Si vinieran de pocos consumidores internos detrás de las mismas
IP, ese límite los bloquearía; por eso es una variable (`waf_rate_limit`) y la
prueba de carga exime a sus generadores (`load_test_mode`). En producción lo
correcto sería limitar **por API key** en lugar de por IP: cada consumidor tiene
su cuota sin importar desde dónde llama.

### Producción y demo

El diseño y los valores por defecto de la IaC describen **producción**. Lo que está
desplegado para el reto es esa misma arquitectura con algunas reducciones
deliberadas, para no pagar disponibilidad que una demo de pocas horas no necesita.
Cada diferencia es un parámetro o una decisión documentada, no una omisión.

| Aspecto | Producción (el diseño) | Demo (lo desplegado) | Cómo se cambia |
|---|---|---|---|
| **NAT Gateway** | Uno por zona: la caída de una zona no deja a las demás sin salida | Uno solo | `nat_per_az` en `iac/stacks/platform/terraform.tfvars` |
| **Entornos** | Staging y producción en cuentas de AWS separadas | Un clúster, dos namespaces | Aplicar el stack `platform` una vez por cuenta |
| **Vida de la plataforma** | Permanente, con protección contra borrado en el ALB | Efímera: se apaga con `ops-down` | Activar `enable_deletion_protection` |
| **Access logs del ALB** | Activos, con retención corta o muestreo | Desactivados | Agregar el bucket y el bloque `access_logs` |
| **Endpoint del clúster** | Restringido a rangos conocidos o privado | Público (protegido por IAM) | `endpoint_public_access_cidrs` |
| **Permisos del pipeline de IaC** | Acotados con un *permission boundary*; rol de solo lectura para el plan | `AdministratorAccess`, limitado por la confianza OIDC y la aprobación manual | Stack `bootstrap` |
| **Límite del WAF** | Por API key | Por IP | Regla de la web ACL |
| **Autenticación** | JWT (Cognito, máquina a máquina) | API key | Ver sección 1 |
| **Alertas** | Por consumo del presupuesto de error | Por umbral (CloudWatch) | Ver sección 3 |
| **Cifrado** | Llaves KMS propias donde haya requisito de cumplimiento | Llaves administradas por AWS | Excepciones justificadas junto a cada recurso |

Lo que **no** se redujo, porque es justo lo que el reto evalúa: tres zonas, mínimo
de tres réplicas repartidas, HTTPS únicamente, WAF, subnets privadas, secretos
fuera del código, autoescalado y rollback.

### Alternativas consideradas

| Decisión | Se eligió | Alternativa | Por qué |
|---|---|---|---|
| Cómputo | **EKS (Auto Mode)** | ECS Fargate | Fargate es menos operación y sería una elección válida para un solo servicio. Se eligió EKS porque se parece a lo que hay en producción en la mayoría de equipos de plataforma, y porque la API expone justamente el estado de un clúster. Auto Mode quita lo más pesado de operar Kubernetes: nodos, parches y escalado. |
| Entrada | **ALB + WAF** | API Gateway | Con el cómputo en EKS, API Gateway no reemplaza al balanceador: necesita uno detrás (VPC Link), así que se suma una capa. Cobra por petición: a 10.000 RPS, incluso por pocas horas al día, son miles de millones de peticiones al mes y resulta órdenes de magnitud más caro que un ALB. Su cuota por defecto es justo 10.000 RPS. Lo que se pierde: cuotas por consumidor y autenticación en el borde. Se agregaría delante del ALB si eso fuera un requisito. |
| Entrada | **ALB + WAF** | CloudFront delante | Las respuestas cambian cada 15 segundos y van autenticadas: hay poco que cachear. Sería un componente más sin beneficio claro. |
| Nodos | **Auto Mode** | Node groups administrados | Menos piezas que mantener (sin Karpenter ni controlador de balanceadores instalados a mano). El costo es un recargo sobre el precio de las instancias. |
| Entornos | **Un clúster, dos namespaces** | Un clúster por entorno | Un segundo plano de control duplica el costo fijo. Ver la sección de multi-entorno. |
| Persistencia | **Ninguna** | DynamoDB / Redis | Ver sección 1. |

### Riesgo conocido

Los pods se registran en el ALB mediante `TargetGroupBinding` sobre un target group
que crea Terraform. Así el balanceador, el WAF y el DNS quedan en la IaC y no
dependen de que exista un Ingress. Si esa combinación diera problemas con Auto
Mode, el plan B es node groups administrados con el AWS Load Balancer Controller.

## 3. Lo que se preguntará igual

### Paridad entre entornos sin datos sensibles en staging

- **Mismo artefacto:** la imagen que se prueba en staging es, byte a byte, la que
  llega a producción (tag inmutable por commit; se construye una sola vez).
- **Mismo chart y misma IaC:** las diferencias entre entornos caben en dos archivos
  de valores y son solo de tamaño (réplicas mínimas y máximas).
- **Secretos separados:** cada entorno tiene su API key y su rol de IAM. El pod de
  staging no puede leer el secreto de producción.
- **Sin datos sensibles que copiar:** la API no guarda datos de clientes. En un
  sistema con base de datos, staging usaría datos sintéticos o anonimizados
  generados por un job, nunca una copia de producción.

El trade-off honesto: staging y producción comparten clúster y balanceador. Están
aislados por namespace, permisos y target group, pero un problema del clúster
afecta a los dos, y una actualización de Kubernetes no se puede probar primero en
staging. Con más presupuesto, el mismo stack `platform` se aplicaría dos veces (dos
cuentas de AWS), sin cambiar código.

### SLOs, SLIs y alertas

| SLI | Cómo se mide | SLO (30 días) |
|---|---|---|
| **Disponibilidad** | Peticiones no-5xx / total, medido en el ALB | 99,9 % (43 minutos de presupuesto de error) |
| **Latencia** | p95 del tiempo de respuesta en el ALB | 95 % de las peticiones < 300 ms |
| **Frescura** | Tiempo desde la última recolección exitosa (`collector_last_success_timestamp_seconds`) | 99 % del tiempo < 60 s |

Se mide en el ALB y no en la aplicación porque ahí se ve lo que vive el cliente,
incluso cuando no hay pods que respondan. La frescura es propia de este diseño:
una API que responde 200 con datos de hace una hora está, en la práctica, caída.

**Estrategia de alertas: por consumo del presupuesto de error, no por umbrales.**

| Situación | Ventana | Acción |
|---|---|---|
| Consumo muy rápido (se acabaría el presupuesto en ~2 días) | 1 h, confirmada en 5 min | Despierta a alguien |
| Consumo sostenido (se acabaría en ~5 días) | 6 h, confirmada en 30 min | Despierta a alguien |
| Consumo lento | 3 días | Ticket para el siguiente día hábil |

Regla para una sola persona: **solo despierta lo que afecta al usuario y exige
actuar ya**. Un pod reiniciándose o un nodo que se reemplaza no despiertan a nadie
si el servicio sigue respondiendo; aparecen en `/v1/alerts` y en Grafana.

Lo implementado hoy es la base: alarmas de CloudWatch por umbral sobre los mismos
SLIs. Las alertas por consumo son el siguiente paso sobre esas métricas.

**Tracing:** no está implementado. El diseño sería OpenTelemetry con
auto-instrumentación de FastAPI y httpx, exportando a X-Ray mediante el colector
de AWS, propagando el `X-Amzn-Trace-Id` que ya agrega el ALB. En esta API el valor
está en el recolector (llamadas a Kubernetes y a AWS), más que en las peticiones,
que no salen de memoria. Los logs deberían llevar ese mismo identificador.

### El costo como variable de diseño

Decisiones tomadas pensando en costo:

- **Graviton (arm64) y Spot** con On-Demand de respaldo: la API no tiene estado,
  perder un nodo no pierde nada.
- **NAT Gateway como decisión explícita:** producción usa uno por zona; la demo usa
  uno solo. Son ~65 USD al mes de diferencia, y es disponibilidad que se compra
  (ver "Producción y demo").
- **Sin base de datos** ni caché administrada.
- **Un clúster para los dos entornos.**
- **Tope de 200 vCPU** en el pool de nodos y máximo de réplicas en el HPA: un error
  o un ataque no escalan sin límite.
- **Interruptores** (`PLATFORM_ENABLED`, workflow `ops-down`): lo que cuesta por
  hora se apaga y se vuelve a crear desde cero con el pipeline.
- **El presupuesto es parte del producto:** `/v1/budget` y la alerta de presupuesto
  en riesgo.

Orden de magnitud mensual en reposo (us-east-2, precios de lista, a confirmar con
la calculadora de AWS):

| Componente | USD/mes aprox. |
|---|---|
| Plano de control de EKS | 73 |
| Nodos (≈3 instancias Graviton pequeñas, mayormente Spot, más recargo de Auto Mode) | 50–90 |
| NAT Gateway (uno en la demo; tres en producción ≈ 99) | 33 + tráfico |
| ALB | 17 + uso |
| WAF (web ACL y reglas) | 8 + 0,60 por millón de peticiones |
| Route 53, Secrets Manager, ECR, alarmas | < 5 |
| **Total en reposo (demo)** | **≈ 190–230** |
| **Total en reposo (producción, tres NAT)** | **≈ 255–295** |

Lo que conviene saber antes de que llegue la factura: a **10.000 RPS sostenidos**
el costo dominante deja de ser el cómputo. El WAF cobra por petición, y 10.000 RPS
todo el mes son unos 26.000 millones de peticiones: del orden de 15.000 USD solo
en WAF. Para tráfico interno a ese volumen, lo razonable sería no pasar por WAF
(entrada privada) o aplicar el WAF solo a la entrada pública. Diseñar para
soportar un pico de 10.000 RPS no es lo mismo que pagarlo todo el mes.

### Un cambio crítico en producción fuera de horario

Principios:

1. **Primero revertir, después arreglar.** A las 3 a. m. lo seguro es volver a la
   última versión que funcionó (workflow `rollback`, un botón, sin construir nada).
   La corrección de fondo se hace de día.
2. **El mismo camino de siempre.** Un cambio urgente pasa por el pipeline igual que
   uno normal: pruebas, escaneo, staging y aprobación. Tarda minutos; saltárselo
   es como se convierte un incidente en dos.
3. **Nadie cambia producción a mano.** El pipeline es el único con permiso de
   despliegue. Existe un acceso de emergencia de administrador (*break-glass*) para
   cuando el propio pipeline es el problema; usarlo deja rastro en CloudTrail y
   obliga a una revisión posterior.
4. **Todo queda registrado:** quién aprobó, qué commit, qué imagen y cuándo
   (historial de despliegues de GitHub, historial de Helm y `/v1/deployments`).

Criterio para decidir si un cambio se hace fuera de horario: solo si **resuelve un
incidente en curso o una vulnerabilidad que se está explotando**. Todo lo demás
espera al horario hábil, por urgente que parezca.

El límite real con una sola persona: quien propone el cambio es quien lo aprueba.
La aprobación manual no es una segunda opinión, es una pausa deliberada. Lo que sí
protege es lo automático: pruebas obligatorias, staging primero, rollback
automático y alarmas. Con un segundo integrante, lo primero que se cambiaría es
exigir que apruebe otra persona (`prevent_self_review`) y proteger la rama `main`.

## 4. Con más tiempo

- JWT con Cognito en lugar de API key.
- Alertas por consumo del presupuesto de error y tracing con OpenTelemetry.
- Despliegue canary (Argo Rollouts) en lugar de gradual: hoy el rollback es rápido,
  pero una versión mala llega a todo el tráfico antes de detectarse.
- Un rol de IAM de solo lectura para el `plan` de Terraform en pull requests, y un
  *permission boundary* para el rol que aplica.
- Entornos en cuentas de AWS separadas.
- Access logs del ALB con muestreo.
