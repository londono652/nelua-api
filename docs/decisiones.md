# Decisiones de diseño

El enunciado plantea que una sola persona va a operar esto, con un presupuesto
razonable y sin ganas de que la despierten a las 3 de la mañana. Usé esa idea como
criterio de desempate: cuando dos opciones servían, me quedé con la que deja menos
cosas que vigilar.

## 1. La API

### Qué expone

El dominio era libre. Pensé en lo que alguien de guardia quiere saber cuando abre
el computador y armé los endpoints alrededor de esas preguntas. Los datos son
reales, no de ejemplo.

| Pregunta | Endpoint | De dónde sale el dato |
|---|---|---|
| ¿Cómo está todo? | `/v1/summary` | Agregado de los otros tres |
| ¿Hay algo roto ahora? | `/v1/alerts` | Reglas sobre el estado del clúster y del presupuesto, más los eventos `Warning` de Kubernetes |
| ¿Qué hay desplegado y qué cambió? | `/v1/deployments` | API de Kubernetes: deployments, pods, autoescaladores y ReplicaSets (de ahí sale el historial) |
| ¿Cuánto llevamos gastado? | `/v1/budget` | AWS Budgets |

Con esto quedan cubiertos los ejemplos que da el enunciado: estado de servicios,
eventos de deploy y alertas. El costo entra por el lado del presupuesto.

### Python con FastAPI

En FastAPI los mismos tipos sirven para validar la entrada y para generar la
documentación OpenAPI, así que hay menos código y el contrato no se desactualiza.
Es asíncrono, y este servicio solo hace entrada y salida. También pesó que Python
es lo que suele manejar un equipo de infraestructura, de modo que quien herede la
API la puede leer y modificar.

Un proceso de Python rinde menos que uno de Go o Rust. Lo compenso con el diseño
de la foto en memoria, que explico más abajo, y escalando con más pods.

### Sin base de datos

El clúster ya sabe qué está desplegado y qué revisiones hubo. AWS ya sabe cuánto
se ha gastado. Copiar eso a una base de datos propia me obligaba a respaldarla,
migrarla y vigilarla, y encima podía quedar desactualizada frente al original.

Lo que pierdo es historia. El historial de despliegues llega hasta donde lo guarda
Kubernetes, que son las últimas 10 revisiones por servicio, y las alertas no
guardan pasado. Si algún día hiciera falta un historial largo, le pondría una
tabla de DynamoDB alimentada desde el pipeline. Por ahora no la necesito.

### La foto en memoria

Esta es la decisión que sostiene los 10.000 RPS. Las peticiones nunca consultan a
Kubernetes ni a AWS. Un recolector corre en segundo plano, refresca una foto del
estado cada 15 segundos (cada 15 minutos en el caso del presupuesto) y los
endpoints responden con lo que hay en memoria.

Así, 10 peticiones por segundo o 10.000 le generan la misma carga a las fuentes:
una consulta por pod en cada ciclo. Sin eso, la API le pasaría todo su tráfico al
plano de control del clúster.

Si una fuente falla, la API sigue sirviendo la última foto buena, la marca como
`stale` y lo reporta como alerta. El readiness tampoco depende de las fuentes en
cada llamada, así que un fallo corto de AWS Budgets no saca los pods del balanceador.

A cambio, los datos pueden tener hasta 15 segundos de antigüedad. Cada respuesta
dice cuándo se tomaron, en `meta.collected_at`.

### REST y no GraphQL

Son cuatro recursos de solo lectura con pocos filtros. Con REST tengo códigos de
estado, caché y herramientas como curl, k6 o el WAF sin agregar nada. GraphQL
tiene sentido cuando los clientes necesitan armar consultas sobre un grafo grande.
Aquí habría sido más complejidad, y además se vuelve más difícil limitar cuánto
cuesta una consulta.

### Errores

Todos los errores salen con el mismo formato, el de la RFC 9457
(`application/problem+json`), con los campos `type`, `title`, `status`, `detail` e
`instance`. Vale para 401, 404, 405, 422, 500 y 503. Los errores inesperados no
muestran detalles internos.

### Autenticación con API key

Usé API key porque los consumidores son pocos y la API es interna y de solo
lectura. La llave no está en el código ni en el repositorio. Vive en Secrets
Manager, hay una por entorno, la API la vuelve a leer cada 5 minutos (se puede
rotar sin redesplegar) y la comparación se hace en tiempo constante.

Nadie tiene que mandarle la llave a nadie. El recorrido es este:

1. Terraform genera una llave aleatoria por entorno y la guarda en Secrets Manager.
2. El pod la lee con su rol de IAM (Pod Identity). Al chart y al pipeline solo les
   llega el nombre del secreto, nunca el valor.
3. Cada consumidor la lee del mismo secreto con su propio permiso de IAM.
4. La llave viaja en el encabezado `X-API-Key`, siempre sobre HTTPS.

En la práctica, quien controla el acceso es IAM. Puede usar la API quien tenga
permiso de leer ese secreto, y CloudTrail deja registro de quién lo leyó. La API
tiene una dirección en internet porque el reto pide exposición pública, pero eso
no la deja abierta: sin llave solo responden los endpoints de salud.

La limitación es que todos comparten el mismo secreto. No expira y no dice quién
llamó. Lo que haría después es pasar a JWT emitido por Cognito en flujo máquina a
máquina, con tokens de una hora y un cliente por consumidor, validado en la API o
en el ALB. No lo metí porque es un componente más que operar y no cambia lo que el
reto evalúa.

## 2. La arquitectura

![Arquitectura de producción en AWS](arquitectura.png)

| Requisito | Cómo se cumple |
|---|---|
| Exposición pública segura | Solo está abierto el puerto 443; el 80 no, porque es una API y no un sitio. TLS 1.2 o superior con certificado de ACM. WAF con límite por IP y reglas administradas de AWS. Los pods no tienen IP pública y solo aceptan tráfico del ALB. La aplicación exige API key. `/metrics` no se entrega desde internet, lo lee Prometheus dentro del clúster. |
| Alta disponibilidad | Tres zonas. En producción hay mínimo tres réplicas repartidas entre ellas, un PodDisruptionBudget y despliegue gradual con `maxUnavailable: 0`. |
| Escalabilidad a 10.000 RPS | La API no tiene estado y responde desde memoria. El HPA escala por CPU, rápido hacia arriba y lento hacia abajo. EKS Auto Mode agrega nodos cuando hay pods que no caben. |
| Tolerancia a fallos | Probes de arranque, readiness y liveness. Apagado ordenado: el pod sigue atendiendo mientras el ALB lo retira. La foto en memoria aísla de fallos de las fuentes. Nodos Spot con On-Demand de respaldo. |
| Manejo de secretos | Las API keys están en Secrets Manager y su valor no pasa por el repositorio, el pipeline ni el estado de Terraform. No hay llaves de AWS guardadas: GitHub entra por OIDC y la API por Pod Identity. |
| Monitoreo básico | Alarmas de CloudWatch sobre el ALB (5xx, latencia p95 y pods fuera de servicio) con aviso por SNS. Prometheus y Grafana en el clúster. Y la propia API, en `/v1/alerts`. |

### El pico de 10.000 RPS

El enunciado habla de un pico, no de tráfico constante, y eso cambia cómo se
escala y cuánto cuesta.

Un pico es sobre todo un problema de velocidad. El HPA tarda cerca de un minuto en
reaccionar y un nodo nuevo tarda otro minuto. En ese rato solo atienden los pods
que ya existían. Por eso el mínimo de réplicas tiene que alcanzar para el tráfico
que puede llegar mientras el clúster escala. Ese número sale de la prueba de carga
(cuántos RPS aguanta un pod) y se le deja margen: el HPA escala al 60 % de CPU.

Cuando el pico pasa, el HPA baja despacio para no oscilar y los nodos vacíos se
eliminan. La capacidad extra se paga mientras se usa.

El enunciado no dice cuánto dura el pico ni si es predecible. Asumí que es diario
y de pocas horas. La arquitectura es la misma en cualquier caso y lo que cambia
son parámetros de operación:

| Si el pico es... | Qué se ajusta | Dónde |
|---|---|---|
| Diario y predecible | Subir el mínimo de réplicas antes de la hora del pico (escalado programado) | Una regla sobre el HPA |
| Impredecible | Mantener un mínimo de réplicas más alto todo el tiempo | `api/chart/values-prod.yaml` |
| Sostenido muchas horas al día | Pasar parte de los nodos de Spot a On-Demand con descuento por compromiso, y revisar el costo del WAF por petición | Pool de nodos y estimación de costo |

Hay otro supuesto en el WAF. El límite es de 2.000 peticiones por IP cada 5
minutos, que protege contra abuso desde una sola dirección y da por hecho que los
10.000 RPS vienen de muchos clientes. Si vinieran de pocos consumidores internos
detrás de las mismas IP, ese límite los bloquearía. Por eso es una variable
(`waf_rate_limit`) y la prueba de carga exime a sus generadores con
`load_test_mode`. En producción limitaría por API key y no por IP, para que cada
consumidor tenga su cuota sin importar desde dónde llama.

### Producción y demo

El diseño y los valores por defecto de la IaC son los de producción. Lo que
desplegué para el reto es la misma arquitectura con algunas cosas reducidas, para
no pagar disponibilidad que una demo de pocas horas no necesita. Cada diferencia
es un parámetro o está explicada aquí.

| Aspecto | Producción | Demo | Cómo se cambia |
|---|---|---|---|
| NAT Gateway | Uno por zona, para que la caída de una zona no deje a las otras sin salida | Uno solo | `nat_per_az` en `iac/stacks/platform/terraform.tfvars` |
| Entornos | Staging y producción en cuentas de AWS separadas | Un clúster con dos namespaces | Aplicar el stack `platform` una vez por cuenta |
| Vida de la plataforma | Permanente, con protección contra borrado en el ALB | Se apaga con `ops-down` | Activar `enable_deletion_protection` |
| Access logs del ALB | Activos, con retención corta o muestreo | Desactivados | Agregar el bucket y el bloque `access_logs` |
| Endpoint del clúster | Restringido a rangos conocidos, o privado | Público, protegido por IAM | `endpoint_public_access_cidrs` |
| Permisos del pipeline de IaC | Acotados con un *permission boundary* y un rol de solo lectura para el plan | `AdministratorAccess`, limitado por la confianza OIDC y la aprobación manual | Stack `bootstrap` |
| Límite del WAF | Por API key | Por IP | Regla de la web ACL |
| Autenticación | JWT con Cognito, máquina a máquina | API key | Sección 1 |
| Alertas | Por consumo del presupuesto de error | Por umbral, en CloudWatch | Sección 3 |
| Cifrado | Llaves KMS propias donde haya requisito de cumplimiento | Llaves administradas por AWS | Excepciones justificadas junto a cada recurso |

No reduje nada de lo que el reto evalúa: siguen las tres zonas, el mínimo de tres
réplicas repartidas, HTTPS como único protocolo, el WAF, las subnets privadas, los
secretos fuera del código, el autoescalado y el rollback.

### Alternativas que consideré

| Decisión | Elegí | Alternativa | Por qué |
|---|---|---|---|
| Cómputo | EKS Auto Mode | ECS Fargate | Fargate da menos trabajo y para un solo servicio habría sido una elección válida. Me fui por EKS porque se parece a lo que corre en producción en la mayoría de equipos de plataforma, y porque la API expone el estado de un clúster. Auto Mode se encarga de nodos, parches y escalado, que es lo más pesado de operar en Kubernetes. |
| Entrada | ALB con WAF | API Gateway | Con el cómputo en EKS, API Gateway no reemplaza al balanceador. Necesita uno detrás (VPC Link), así que sería una capa más. Cobra por petición, y a 10.000 RPS, aunque sea pocas horas al día, son miles de millones de peticiones al mes: sale órdenes de magnitud más caro que un ALB. Su cuota por defecto es de 10.000 RPS, el mismo número del pico. Pierdo cuotas por consumidor y autenticación en el borde. Si eso fuera un requisito, lo pondría delante del ALB. |
| Entrada | ALB con WAF | CloudFront delante | Las respuestas cambian cada 15 segundos y van autenticadas, así que casi no hay qué cachear. |
| Nodos | Auto Mode | Node groups administrados | Hay menos piezas que mantener, porque no instalo Karpenter ni el controlador de balanceadores. Se paga un recargo sobre el precio de las instancias. |
| Entornos | Un clúster, dos namespaces | Un clúster por entorno | Un segundo plano de control duplica el costo fijo. Lo amplío en la sección 3. |
| Persistencia | Ninguna | DynamoDB o Redis | Sección 1. |

### Un riesgo que tengo identificado

Los pods se registran en el ALB con un `TargetGroupBinding` sobre un target group
que crea Terraform. Lo hice así para que el balanceador, el WAF y el DNS queden en
la IaC y no dependan de que exista un Ingress. Si esa combinación diera problemas
con Auto Mode, pasaría a node groups administrados con el AWS Load Balancer Controller.

## 3. Los temas "deseables"

### Paridad entre entornos sin datos sensibles en staging

La imagen que se prueba en staging es la misma que llega a producción. Se
construye una sola vez y el tag es el SHA del commit, que ECR no deja sobrescribir.
El chart y la IaC también son los mismos. Las diferencias entre entornos caben en
dos archivos de valores y son de tamaño: réplicas mínimas y máximas.

Los secretos sí están separados. Cada entorno tiene su API key y su rol de IAM, y
el pod de staging no puede leer el secreto de producción.

Esta API no guarda datos de clientes, así que no hay nada sensible que copiar. En
un sistema con base de datos, staging trabajaría con datos sintéticos o
anonimizados generados por un job, nunca con una copia de producción.

Lo que no está resuelto es el aislamiento. Staging y producción comparten clúster
y balanceador. Los separan el namespace, los permisos y el target group, pero un
problema del clúster afecta a los dos, y una actualización de Kubernetes no se
puede probar primero en staging. Con más presupuesto aplicaría el mismo stack
`platform` en dos cuentas de AWS, sin tocar el código.

### SLOs, SLIs y alertas

| SLI | Cómo se mide | SLO (30 días) |
|---|---|---|
| Disponibilidad | Peticiones sin 5xx sobre el total, medido en el ALB | 99,9 % (43 minutos de presupuesto de error) |
| Latencia | p95 del tiempo de respuesta en el ALB | 95 % de las peticiones por debajo de 300 ms |
| Frescura | Tiempo desde la última recolección exitosa (`collector_last_success_timestamp_seconds`) | Menos de 60 s el 99 % del tiempo |

Mido en el ALB porque ahí se ve lo que le pasa al cliente, incluso cuando no hay
ningún pod respondiendo. La frescura la agregué por cómo está hecha la API: si
responde 200 con datos de hace una hora, para quien la consulta está caída.

Las alertas las plantearía por consumo del presupuesto de error y no por umbrales:

| Situación | Ventana | Acción |
|---|---|---|
| Consumo muy rápido (el presupuesto se acabaría en unos 2 días) | 1 h, confirmada en 5 min | Despierta a alguien |
| Consumo sostenido (se acabaría en unos 5 días) | 6 h, confirmada en 30 min | Despierta a alguien |
| Consumo lento | 3 días | Ticket para el siguiente día hábil |

Con una sola persona de guardia, la regla es que solo la despierte algo que afecta
al usuario y que hay que atender ya. Un pod que se reinicia o un nodo que se
reemplaza no cumplen eso si el servicio sigue respondiendo. Esos casos quedan en
`/v1/alerts` y en Grafana.

Hoy lo que está implementado son alarmas de CloudWatch por umbral sobre esos
mismos indicadores. Las alertas por consumo serían el paso siguiente.

El tracing no está implementado. Lo haría con OpenTelemetry, usando la
auto-instrumentación de FastAPI y httpx, exportando a X-Ray con el colector de AWS
y propagando el `X-Amzn-Trace-Id` que ya agrega el ALB. En esta API serviría más
en el recolector, que es el que llama a Kubernetes y a AWS, que en las peticiones,
porque esas no salen de memoria. Los logs deberían llevar el mismo identificador.

### El costo como variable de diseño

Varias decisiones salieron de mirar el costo. Los nodos son Graviton y Spot, con
On-Demand de respaldo, porque la API no tiene estado y perder un nodo no pierde
nada. No hay base de datos ni caché administrada. Los dos entornos comparten clúster.

El NAT Gateway es el caso más claro: producción lleva uno por zona y la demo uno
solo. La diferencia son unos 65 USD al mes, que es lo que cuesta esa disponibilidad.

También puse topes. El pool de nodos no pasa de 200 vCPU y el HPA tiene un máximo
de réplicas, para que un error o un ataque no escalen sin límite. Y todo lo que
cuesta por hora se puede apagar con `PLATFORM_ENABLED` y el workflow `ops-down`, y
volver a crear desde cero con el pipeline.

Por último, el gasto se puede consultar en la misma API, en `/v1/budget`, y hay
una alerta cuando el presupuesto está en riesgo.

Estos son órdenes de magnitud mensuales en reposo, en us-east-2 y con precios de
lista. Habría que confirmarlos con la calculadora de AWS.

| Componente | USD/mes aprox. |
|---|---|
| Plano de control de EKS | 73 |
| Nodos (unas 3 instancias Graviton pequeñas, casi todas Spot, más el recargo de Auto Mode) | 50 a 90 |
| NAT Gateway (uno en la demo; tres en producción serían unos 99) | 33 más tráfico |
| ALB | 17 más uso |
| WAF (web ACL y reglas) | 8, más 0,60 por millón de peticiones |
| Route 53, Secrets Manager, ECR y alarmas | menos de 5 |
| Total en reposo, demo | 190 a 230 |
| Total en reposo, producción con tres NAT | 255 a 295 |

Hay un dato que vale la pena tener presente. Con 10.000 RPS sostenidos, lo más
caro ya no es el cómputo. El WAF cobra por petición, y 10.000 RPS durante todo el
mes son unos 26.000 millones de peticiones, del orden de 15.000 USD solo en WAF.
Si ese volumen fuera tráfico interno, convendría no pasarlo por el WAF (con una
entrada privada) o dejar el WAF solo en la entrada pública.

### Un cambio crítico en producción fuera de horario

A las 3 a. m. lo primero es revertir. Volver a la última versión que funcionó es
un botón (el workflow `rollback`) y no construye nada. El arreglo de fondo se hace
de día.

Si de todos modos hay que sacar un cambio, va por el pipeline igual que cualquier
otro: pruebas, escaneo, staging y aprobación. Son minutos, y saltárselo es la
forma más fácil de terminar con dos incidentes en vez de uno.

Nadie cambia producción a mano. El único que tiene permiso de despliegue es el
pipeline. Existe un acceso de administrador para emergencias (*break-glass*), para
cuando el problema es el pipeline mismo. Usarlo queda en CloudTrail y obliga a
revisar después qué pasó.

Todo queda registrado: quién aprobó, qué commit, qué imagen y a qué hora. Está en
el historial de despliegues de GitHub, en el de Helm y en `/v1/deployments`.

¿Cuándo se justifica un cambio fuera de horario? Solo cuando resuelve un incidente
en curso o una vulnerabilidad que ya están explotando. Lo demás espera al horario
hábil, aunque parezca urgente.

Con una sola persona hay un límite que no se puede esconder: el que propone el
cambio es el mismo que lo aprueba. La aprobación manual en ese caso es una pausa
para pensar, no una segunda opinión. Lo que protege de verdad es lo automático:
las pruebas obligatorias, pasar primero por staging, el rollback y las alarmas.
Con una segunda persona en el equipo, lo primero que cambiaría es exigir que
apruebe alguien distinto (`prevent_self_review`) y proteger la rama `main`.

## 4. Con más tiempo

- JWT con Cognito en lugar de API key.
- Alertas por consumo del presupuesto de error y tracing con OpenTelemetry.
- Despliegue canary con Argo Rollouts. Hoy el rollback es rápido, pero una versión
  mala alcanza a recibir todo el tráfico antes de que se detecte.
- Un rol de IAM de solo lectura para el `plan` de Terraform en pull requests, y un
  *permission boundary* para el rol que aplica.
- Entornos en cuentas de AWS separadas.
- Access logs del ALB con muestreo.
