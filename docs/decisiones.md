# Decisiones de diseño

El enunciado plantea que una sola persona va a operar esto, con un presupuesto
razonable y sin ganas de que la despierten a las 3 de la mañana. Usé esa idea como
criterio de desempate: cuando dos opciones servían, me quedé con la que deja menos
cosas que vigilar.

## 1. La API

### Qué expone

El dominio era libre. Elegí lo que le pregunta un equipo de plataforma a su
tablero: cómo vienen saliendo los despliegues y en qué estado están los servicios.

| Pregunta | Endpoint | De dónde sale el dato |
|---|---|---|
| ¿Qué repos se siguen? | `/v1/repos` | Configuración (`githubRepos`) |
| ¿Qué se desplegó hace poco? | `/v1/repos/{owner}/{repo}/deploys` | GitHub Deployments API. Cada job del pipeline con `environment: staging` o `prod` deja un registro con su estado |
| ¿Qué tan bien salen los despliegues? | `/v1/repos/{owner}/{repo}/deploys/stats` | Calculado sobre el historial guardado en DynamoDB: tasa de éxito, tasa de fallos, despliegues por día y duración, en 7 y 30 días |
| ¿Cómo están los servicios? | `/v1/deployments` | API de Kubernetes: deployments, pods, autoescaladores y ReplicaSets (de ahí sale el historial de revisiones y los rollbacks) |

Con esto quedan cubiertos dos de los ejemplos del enunciado: estado de servicios y
eventos de deploy. La tasa de fallos de despliegue es una de las cuatro métricas
DORA, así que también son métricas de entrega y no solo de infraestructura.

Si un job se cancela, GitHub puede dejar el despliegue "en curso" para siempre.
Pasadas 24 horas el recolector deja de preguntar por él, para no gastar el límite.

Cómo cuento los estados de GitHub: `success` e `inactive` son éxitos (`inactive`
es un despliegue que salió bien y luego fue reemplazado por otro más nuevo),
`failure` y `error` son fallos, y lo demás está en curso. La tasa se calcula solo
sobre los que terminaron.

### Parametrizable por repo, pero con lista cerrada

La ruta lleva `owner` y `repo`, así que la misma API sirve para cualquier
repositorio. Pero solo responde por los que están en la lista `githubRepos` del
chart. Cualquier otro devuelve 404 sin llegar a GitHub.

La razón es el límite de GitHub: 5.000 peticiones por hora por token. Si la API
aceptara cualquier `owner/repo` y fuera a buscarlo, un cliente (o un ataque) podría
agotar ese límite pidiendo repos al azar, y a 10.000 RPS eso pasa en un segundo.
Con la lista, lo que se le pide a GitHub depende solo de cuántos repos hay
configurados, no del tráfico. Agregar uno es un cambio de valores que sale por el
pipeline.

### Python con FastAPI

En FastAPI los mismos tipos sirven para validar la entrada y para generar la
documentación OpenAPI, así que hay menos código y el contrato no se desactualiza.
Es asíncrono, y este servicio solo hace entrada y salida. También pesó que Python
es lo que suele manejar un equipo de infraestructura, de modo que quien herede la
API la puede leer y modificar.

Un proceso de Python rinde menos que uno de Go o Rust. Lo compenso con el diseño
de la foto en memoria, que explico más abajo, y escalando con más pods.

### Recolector, DynamoDB y foto en memoria

Esta es la decisión que sostiene los 10.000 RPS. Las fuentes son lentas y tienen
límites: GitHub permite 5.000 peticiones por hora y el plano de control de
Kubernetes no está hecho para recibir el tráfico de una API pública. Así que las
peticiones de los clientes nunca llegan a ellas.

```
GitHub ─┐                         ┌─ pod API ─┐
        ├─> recolector ─> DynamoDB├─ pod API ─┼─> ALB ─> clientes
K8s   ──┘   (1 réplica)           └─ pod API ─┘
            cada 15 s / 60 s       cada pod lee cada 5 s
                                   y responde desde memoria
```

- **El recolector** es un Deployment aparte, con una sola réplica, que usa la
  misma imagen. Consulta Kubernetes cada 15 segundos y GitHub cada 60. Guarda en
  DynamoDB el historial de despliegues y una foto ya calculada de cada vista
  (los últimos 100 despliegues y las métricas de cada ambiente y ventana).
- **DynamoDB** es la memoria compartida. El historial se guarda porque GitHub solo
  devuelve las últimas páginas y la tasa de éxito necesita 30 días. Además, el
  recolector no vuelve a preguntar por un despliegue que ya terminó: lo tiene
  guardado. Cada registro expira a los 90 días (TTL).
- **Los pods de la API** leen las fotos de DynamoDB cada 5 segundos, con una sola
  lectura en lote, y las sirven desde memoria. Filtrar por ambiente o namespace se
  hace sobre esa foto.

Así, 10 peticiones por segundo o 10.000 generan la misma carga hacia atrás. Con un
repo, GitHub recibe unas 60 a 120 peticiones por hora. Con 30 pods, DynamoDB recibe
unas 6 lecturas por segundo, que bajo demanda cuestan un par de dólares al mes.

**Por qué un recolector aparte y no que cada pod consulte las fuentes.** Con 30
pods, cada uno consultando GitHub cada minuto, son 1.800 peticiones por hora por
repo, todas con el mismo token. Con tres repos ya no alcanza el límite. Y los 30
pods le pegarían al plano de control de Kubernetes cada 15 segundos. Un solo
cliente hacia las fuentes es más fácil de razonar y de limitar.

**Por qué DynamoDB y no Redis.** Necesitaba un lugar compartido donde el
recolector escribe y los pods leen, y que además guarde historia. DynamoDB es
administrado, sin servidor, multi-zona por defecto, con TTL y respaldo continuo, y
bajo demanda cuesta casi nada con esta carga. ElastiCache me daba latencias más
bajas, pero los pods no leen en cada petición sino cada 5 segundos, así que esa
ventaja no se nota, y es un clúster más que dimensionar y pagar por hora.

**Qué pasa si algo falla.**

| Falla | Qué ve el cliente |
|---|---|
| GitHub o Kubernetes no responden | La última foto, con `meta.stale: true` cuando lleva más de 3 ciclos sin renovarse, y en `meta.sync` el motivo y cuántos intentos van fallando |
| Un repo deja de ser accesible | Solo ese repo queda desactualizado; los demás se siguen sincronizando |
| El recolector se cae | La última foto. Kubernetes lo reinicia (su liveness revisa un archivo de latido) |
| DynamoDB no responde | Cada pod sigue sirviendo la foto que tiene en memoria |
| Un pod nuevo arranca con DynamoDB caído | No pasa a ready hasta leer una vez, así que no recibe tráfico |

Servir la foto anterior evita que una caída de GitHub tumbe la API, pero tiene un
riesgo: quedarse sirviendo datos viejos sin que nadie se entere, porque la API
sigue respondiendo 200 y las alarmas del ALB no ven nada raro. Para eso hay tres
capas:

1. **En la respuesta.** `meta.stale` dice si el dato está viejo y `meta.sync` dice
   por qué: último intento, último éxito, fallos seguidos y el error resumido (por
   ejemplo `api.github.com respondió HTTP 401 en /repos/...` cuando vence el token).
2. **En CloudWatch.** El recolector publica la métrica `SyncSuccess` (1 o 0) por
   fuente después de cada ciclo. Dos alarmas avisan por SNS si no hubo una
   sincronización buena con GitHub en 5 minutos o con el clúster en 2. Tratan la
   falta de datos como falla, así que también saltan si el recolector se muere o
   pierde permisos. Están fuera del clúster, igual que las del ALB.
3. **En Grafana.** `snapshot_age_seconds` muestra la antigüedad de cada foto.

La liveness del recolector no depende de que las fuentes respondan, solo de que el
ciclo siga girando. Reiniciarlo no arregla un token vencido; eso lo avisa la alarma.

El readiness no depende de GitHub ni de Kubernetes. Si dependiera, una caída de
GitHub sacaría todos los pods del balanceador, y la API dejaría de responder justo
cuando más se necesita.

A cambio, los datos tienen un retraso: hasta 20 segundos para el clúster (15 del
recolector más 5 de los pods) y hasta 65 para los despliegues. Cada respuesta dice
cuándo se tomaron, en `meta.collected_at`.

**El recolector con una sola réplica** es una decisión consciente. Si se cae, lo
único que se pierde es frescura, y Kubernetes lo levanta en segundos. Dos réplicas
gastarían el doble de cuota de GitHub, o necesitarían elección de líder, para
proteger algo que ya está protegido por la foto. Se despliega con estrategia
`Recreate`, así que nunca hay dos a la vez.

**Seguridad.** La API y el recolector tienen identidades separadas. La API solo
puede leer DynamoDB y su secreto de API keys; no tiene ningún permiso sobre
Kubernetes. El recolector puede escribir en DynamoDB, leer el token de GitHub y
listar deployments, pods, autoescaladores y ReplicaSets en su namespace. Un pod de
la API comprometido no puede escribir datos ni ver el token.

### REST y no GraphQL

Son pocos recursos de solo lectura con pocos filtros. Con REST tengo códigos de
estado, caché y herramientas como curl, k6 o el WAF sin agregar nada. GraphQL
tiene sentido cuando los clientes necesitan armar consultas sobre un grafo grande.
Aquí habría sido más complejidad, y además se vuelve más difícil limitar cuánto
cuesta una consulta.

### Errores

Todos los errores salen con el mismo formato, el de la RFC 9457
(`application/problem+json`), con los campos `type`, `title`, `status`, `detail` e
`instance`. Vale para 401, 404 (incluido un repo que no se monitorea), 405, 422,
500 y 503 (todavía no hay datos recolectados). Los errores inesperados no
muestran detalles internos.

### Autenticación con API key

Usé API key porque los consumidores son pocos y la API es interna y de solo
lectura. La llave no está en el código ni en el repositorio. Vive en Secrets
Manager, hay una por ambiente, la API la vuelve a leer cada 5 minutos (se puede
rotar sin redesplegar) y la comparación se hace en tiempo constante.

Nadie tiene que mandarle la llave a nadie. El recorrido es este:

1. Terraform genera una llave aleatoria por ambiente y la guarda en Secrets Manager.
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

Hay un diagrama por ambiente. Son el mismo diseño con distintos valores.

![Ambiente de staging](arquitectura-staging.png)

![Ambiente de producción](arquitectura-produccion.png)

| Requisito | Cómo se cumple |
|---|---|
| Exposición pública segura | Solo está abierto el puerto 443; el 80 no, porque es una API y no un sitio. TLS 1.2 o superior con certificado de ACM. WAF con límite por IP y reglas administradas de AWS. Los pods no tienen IP pública y solo aceptan tráfico del ALB. La aplicación exige API key. `/metrics` no se entrega desde internet, lo lee Prometheus dentro del clúster. |
| Alta disponibilidad | Tres zonas. En producción hay mínimo tres réplicas repartidas entre ellas, un PodDisruptionBudget y despliegue gradual con `maxUnavailable: 0`. |
| Escalabilidad a 10.000 RPS | La API no tiene estado y responde desde memoria. El HPA escala por CPU, rápido hacia arriba y lento hacia abajo. EKS Auto Mode agrega nodos cuando hay pods que no caben. La carga hacia GitHub, Kubernetes y DynamoDB no crece con el tráfico. |
| Tolerancia a fallos | Probes de arranque, readiness y liveness. Apagado ordenado: el pod sigue atendiendo mientras el ALB lo retira. La foto en memoria aísla de fallos de las fuentes y de DynamoDB. DynamoDB replica en tres zonas y tiene respaldo continuo. Nodos Spot con On-Demand de respaldo. |
| Manejo de secretos | Las API keys y el token de GitHub están en Secrets Manager. Su valor no pasa por el repositorio, el pipeline ni el estado de Terraform. No hay llaves de AWS guardadas: GitHub entra por OIDC y los pods por Pod Identity, con un rol distinto para la API y para el recolector. |
| Monitoreo básico | Alarmas de CloudWatch sobre el ALB (5xx, latencia p95 y pods fuera de servicio) con aviso por SNS. Prometheus y Grafana en el clúster. Alarmas de frescura: avisan si el recolector lleva 5 minutos sin sincronizar con GitHub o 2 con el clúster. Cada respuesta trae `meta.stale` y `meta.sync` con el motivo. |

### El pico de 10.000 RPS

El enunciado habla de un pico, no de tráfico constante, y eso cambia cómo se
escala y cuánto cuesta.

Para tener un orden de magnitud hice una prueba local con k6 contra un solo
proceso de la API, en una máquina de 2 vCPU compartidas con el propio k6. Aguantó
unos 800 RPS con p95 de 18 ms y sin errores. En un pod con 1 vCPU dedicada debería
rendir igual o mejor. Con ese número, 10.000 RPS son unos 13 pods al 100 % de CPU, o
unos 22 al 60 % que usa el HPA, dentro del máximo de 30. La cifra real sale de la
prueba de carga en staging.

Un pico es sobre todo un problema de velocidad. El HPA tarda cerca de un minuto en
reaccionar y un nodo nuevo tarda otro minuto. En ese rato solo atienden los pods
que ya existían. Por eso el mínimo de réplicas tiene que alcanzar para el tráfico
que puede llegar mientras el clúster escala. Ese número sale de la prueba de carga
(cuántos RPS aguanta un pod) y se le deja margen: el HPA escala al 60 % de CPU.

Cuando el pico pasa, el HPA baja despacio para no oscilar y los nodos vacíos se
eliminan. La capacidad extra se paga mientras se usa.

La prueba de carga la corro contra staging. Para que mida lo mismo que mediría en
producción, el script le sube antes el autoescalado a los valores de producción
(`values-loadtest.yaml`) y lo devuelve al terminar. La red y el clúster son
equivalentes; la diferencia que queda es que staging sale por un solo NAT.

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

### Staging y producción

Hay dos ambientes y cada uno tiene su propia red, su clúster, su balanceador y su
WAF. Los crea el mismo stack de Terraform (`iac/stacks/platform`). Lo que cambia
entre uno y otro está en dos archivos de valores, `envs/staging.tfvars` y
`envs/prod.tfvars`, y cada ambiente guarda su estado por separado.

Para el reto desplegué staging, que es donde probé la API de punta a punta.
Producción está definida y el pipeline la planea en cada ejecución, pero la dejé
apagada para no pagar dos clústeres. Encenderla es poner `PROD_ENABLED` en `true`.

| | Staging (desplegado) | Producción (definido) |
|---|---|---|
| Red | VPC `10.0.0.0/16`, tres zonas | VPC `10.1.0.0/16`, tres zonas |
| NAT Gateway | Uno solo | Uno por zona, para que la caída de una zona no deje a las otras sin salida |
| Clúster | EKS Auto Mode propio | EKS Auto Mode propio |
| Réplicas de la API | Mínimo 2, máximo 4 | Mínimo 3 repartidas entre zonas, máximo 30 |
| Balanceador | Sin protección contra borrado, porque se crea y se destruye a demanda | Protegido contra borrado |
| Alarmas | Creadas, sin destinatario | Con aviso por correo (`alert_email`) |
| Vida | Se apaga con `ops-down` | Permanente |

Los dos comparten el registro de imágenes, el certificado y la zona DNS. El
registro tiene que ser compartido para poder promover la misma imagen de un
ambiente al otro.

Staging no se quedó sin lo que el reto evalúa. Tiene tres zonas, HTTPS como único
protocolo, WAF, subnets privadas, secretos fuera del código, autoescalado y
rollback, igual que producción.

Hay una simplificación frente a un entorno real: los dos ambientes viven en la
misma cuenta de AWS, separados por VPC y por nombres. Lo normal es una cuenta por
ambiente. El código no cambiaría, solo las credenciales con las que se aplica.

Y hay cosas que dejaría distintas en producción y que no implementé:

| Aspecto | Hoy, en los dos ambientes | En producción lo cambiaría por |
|---|---|---|
| Access logs del ALB | Desactivados | Activos, con retención corta o muestreo |
| Endpoint del clúster | Público, protegido por IAM | Restringido a rangos conocidos, o privado |
| Permisos del pipeline de IaC | `AdministratorAccess`, limitado por la confianza OIDC y la aprobación manual | Un *permission boundary* y un rol de solo lectura para el plan |
| Límite del WAF | Por IP | Por API key |
| Autenticación | API key | JWT con Cognito, máquina a máquina |
| Alertas | Por umbral, en CloudWatch | Por consumo del presupuesto de error |
| Cifrado | Llaves administradas por AWS | Llaves KMS propias donde haya requisito de cumplimiento |

### Alternativas que consideré

| Decisión | Elegí | Alternativa | Por qué |
|---|---|---|---|
| Cómputo | EKS Auto Mode | ECS Fargate | Fargate da menos trabajo y para un solo servicio habría sido una elección válida. Me fui por EKS porque se parece a lo que corre en producción en la mayoría de equipos de plataforma, y porque la API expone el estado de un clúster. Auto Mode se encarga de nodos, parches y escalado, que es lo más pesado de operar en Kubernetes. |
| Entrada | ALB con WAF | API Gateway | Con el cómputo en EKS, API Gateway no reemplaza al balanceador. Necesita uno detrás (VPC Link), así que sería una capa más. Cobra por petición, y a 10.000 RPS, aunque sea pocas horas al día, son miles de millones de peticiones al mes: sale órdenes de magnitud más caro que un ALB. Su cuota por defecto es de 10.000 RPS, el mismo número del pico. Pierdo cuotas por consumidor y autenticación en el borde. Si eso fuera un requisito, lo pondría delante del ALB. |
| Entrada | ALB con WAF | CloudFront delante | Las respuestas sí se podrían cachear unos segundos, pero la API ya responde desde memoria: sería una caché delante de otra caché. Los consumidores son internos, CloudFront cobra por petición y obliga a incluir la API key en la llave de caché. Lo agregaría si la prueba de carga muestra que los pods no alcanzan. |
| Nodos | Auto Mode | Node groups administrados | Hay menos piezas que mantener, porque no instalo Karpenter ni el controlador de balanceadores. Se paga un recargo sobre el precio de las instancias. |
| Ambientes | Un clúster por ambiente | Un clúster con dos namespaces | Compartir clúster ahorra un plano de control, pero un problema del clúster afecta a los dos ambientes y una actualización de Kubernetes no se puede probar primero en staging. Para no pagar dos, producción queda apagada mientras no se usa. |
| Almacén compartido | DynamoDB bajo demanda | ElastiCache (Redis) o RDS | Sección 1. Los pods leen cada 5 s, no en cada petición, así que la latencia de Redis no se nota. DynamoDB no tiene servidores que dimensionar y guarda la historia con TTL. |
| Fuentes | Un recolector | Cada pod consulta las fuentes | Sección 1. Con 30 pods se multiplica por 30 el consumo del límite de GitHub. |

### Riesgos que tengo identificados

Los pods se registran en el ALB con un `TargetGroupBinding` sobre un target group
que crea Terraform. Lo hice así para que el balanceador, el WAF y el DNS queden en
la IaC y no dependan de que exista un Ingress. Si esa combinación diera problemas
con Auto Mode, pasaría a node groups administrados con el AWS Load Balancer Controller.

El otro es el límite de GitHub. Con un token son 5.000 peticiones por hora y el
recolector gasta unas 60 a 120 por repo. Alcanza para unas 40 repos con margen. Más
allá de eso, la salida es una GitHub App (su límite crece con la organización) o
recibir los eventos por webhook en lugar de consultarlos.

## 3. Los temas "deseables"

### Paridad entre ambientes sin datos sensibles en staging

Los dos ambientes salen del mismo código. La infraestructura es el mismo stack de
Terraform con otro archivo de valores. Los manifiestos del clúster son idénticos,
hasta el namespace se llama igual. El chart es el mismo y la imagen también: se
construye una sola vez, el tag es el SHA del commit y ECR no deja sobrescribirlo.

Lo que cambia entre ambientes es el tamaño (réplicas, cantidad de NAT) y está a la
vista en `envs/*.tfvars` y en `values-*.yaml`. Como cada ambiente tiene su
clúster, una actualización de Kubernetes o un cambio en la red se prueban primero
en staging.

Los secretos están separados. Cada ambiente tiene su API key, su tabla de DynamoDB
y sus roles de IAM, y un pod de staging no puede leer nada de producción. El token
de GitHub sí es uno solo, porque es de solo lectura y no da acceso a AWS.

Esta API no guarda datos de clientes, así que no hay nada sensible que copiar. En
un sistema con base de datos, staging trabajaría con datos sintéticos o
anonimizados generados por un job, nunca con una copia de producción.

Lo que falta para que el aislamiento sea completo es separar las cuentas de AWS.
Hoy los dos ambientes están en la misma.

### SLOs, SLIs y alertas

| SLI | Cómo se mide | SLO (30 días) |
|---|---|---|
| Disponibilidad | Peticiones sin 5xx sobre el total, medido en el ALB | 99,9 % (43 minutos de presupuesto de error) |
| Latencia | p95 del tiempo de respuesta en el ALB | 95 % de las peticiones por debajo de 300 ms |
| Frescura | Antigüedad de la foto que sirve la API (`snapshot_age_seconds`, por vista) | Menos de 60 s el 99 % del tiempo para el clúster y menos de 3 minutos para los despliegues |

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
`/v1/deployments` y en Grafana.

Hoy lo que está implementado son alarmas de CloudWatch por umbral sobre esos
mismos indicadores, incluida la frescura (`SyncSuccess`). Las alertas por consumo serían el paso siguiente.

El tracing no está implementado. Lo haría con OpenTelemetry, usando la
auto-instrumentación de FastAPI y httpx, exportando a X-Ray con el colector de AWS
y propagando el `X-Amzn-Trace-Id` que ya agrega el ALB. En esta API serviría más
en el recolector, que es el que llama a GitHub, a Kubernetes y a DynamoDB, que en
las peticiones, porque esas no salen de memoria. Los logs deberían llevar el mismo identificador.

### El costo como variable de diseño

Varias decisiones salieron de mirar el costo. Los nodos son Graviton y Spot, con
On-Demand de respaldo, porque la API no tiene estado y perder un nodo no pierde
nada. El único almacén es una tabla de DynamoDB bajo demanda, que con esta carga
cuesta pocos dólares al mes, y no hay caché administrada que pagar por hora. Y producción queda apagada
mientras no se usa, para no pagar dos planos de control.

El NAT Gateway es el caso más claro: producción lleva uno por zona y staging uno
solo. La diferencia son unos 65 USD al mes, que es lo que cuesta esa disponibilidad.

También puse topes. El pool de nodos no pasa de 200 vCPU y el HPA tiene un máximo
de réplicas, para que un error o un ataque no escalen sin límite. Y todo lo que
cuesta por hora se puede apagar por ambiente (`STAGING_ENABLED`, `PROD_ENABLED` y
el workflow `ops-down`) y volver a crear desde cero con el pipeline.

Estos son órdenes de magnitud mensuales de un ambiente en reposo, en us-east-2 y
con precios de lista. Habría que confirmarlos con la calculadora de AWS.

| Componente | USD/mes aprox. |
|---|---|
| Plano de control de EKS | 73 |
| Nodos (unas 3 instancias Graviton pequeñas, casi todas Spot, más el recargo de Auto Mode) | 50 a 90 |
| NAT Gateway (uno en staging; los tres de producción serían unos 99) | 33 más tráfico |
| ALB | 17 más uso |
| WAF (web ACL y reglas) | 8, más 0,60 por millón de peticiones |
| DynamoDB bajo demanda (unas 6 lecturas por segundo con 30 pods, menos en reposo) | menos de 5 |
| Métrica `SyncSuccess` (unas 200.000 publicaciones al mes) y sus dos alarmas | unos 3 |
| Route 53, Secrets Manager, ECR y alarmas | menos de 5 |
| Staging en reposo | 190 a 230 |
| Producción en reposo, con tres NAT | 255 a 295 |
| Los dos encendidos | 445 a 525 |

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
el historial de despliegues de GitHub, en el de Helm y en la propia API
(`/v1/repos/{owner}/{repo}/deploys` y `/v1/deployments`).

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
- Una cuenta de AWS por ambiente.
- Access logs del ALB con muestreo.
- Recibir los despliegues de GitHub por webhook en lugar de consultarlos cada
  minuto: datos al instante y casi sin consumo del límite.
