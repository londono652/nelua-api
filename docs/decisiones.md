# Decisiones de diseño

El enunciado plantea que una sola persona va a operar esto, con un presupuesto
razonable y sin ganas de que la despierten a las 3 de la mañana. Usé esa idea como
criterio de desempate: cuando dos opciones servían, me quedé con la que deja menos
cosas que vigilar.

## 1. La API

### Qué expone

El dominio era libre. Elegí lo que le pregunta un equipo de plataforma a su
tablero: cómo vienen saliendo los despliegues, en qué estado están los servicios
y cuánto se lleva gastado.

| Pregunta | Endpoint | De dónde sale el dato |
|---|---|---|
| ¿Qué repos se siguen? | `/v1/repos` | Configuración (`githubRepos`) |
| ¿Qué se desplegó hace poco? | `/v1/repos/{owner}/{repo}/deploys` | GitHub Deployments API. Cada job del pipeline con `environment: staging` o `prod` deja un registro con su estado |
| ¿Qué tan bien salen los despliegues? | `/v1/repos/{owner}/{repo}/deploys/stats` | Calculado sobre el historial guardado en DynamoDB: tasa de éxito, tasa de fallos, despliegues por día y duración, en 7 y 30 días |
| ¿Cuánto llevamos gastado? | `/v1/budget` | AWS Budgets: el presupuesto mensual de la cuenta (lo crea Terraform en la base compartida), con gasto real y pronóstico al cierre |
| ¿Cómo están los servicios? | `/v1/deployments` | API de Kubernetes: deployments, pods, autoescaladores y ReplicaSets (de ahí sale el historial de revisiones y los rollbacks) |

Con esto quedan cubiertos dos de los ejemplos del enunciado, estado de servicios y
eventos de deploy, y uno de los temas deseables: el costo. El presupuesto es de
toda la cuenta, así que staging y producción ven el mismo número. AWS actualiza
el gasto pocas veces al día, por eso el recolector lo consulta cada 15 minutos. La tasa de fallos de despliegue es una de las cuatro métricas
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
  misma imagen. Consulta Kubernetes cada 15 segundos, GitHub cada 60 y AWS
  Budgets cada 15 minutos. Guarda en
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
los presupuestos de la cuenta, y listar deployments, pods, autoescaladores y
ReplicaSets en su namespace. Un pod de
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

### Autenticación: API key en staging, Cognito en producción

Hay dos modos y cada ambiente elige el suyo en Terraform (`auth_mode`). El modo
queda publicado en Parameter Store y el pipeline se lo pasa al chart, así que la
infraestructura y la aplicación no se pueden desalinear.

**Staging: API key.** Es el ambiente de pruebas, con un solo consumidor (yo y el
pipeline). La llave no está en el código ni en el repositorio:

1. Terraform genera una llave aleatoria por ambiente y la guarda en Secrets Manager
   (con un atributo de solo escritura: tampoco queda en el estado).
2. El pod la lee con su rol de IAM (Pod Identity) y la recarga cada 5 minutos, así
   que se rota sin redesplegar. La comparación es en tiempo constante.
3. Quien la usa la lee del secreto con su propio permiso de IAM, y CloudTrail deja
   registro. Viaja en `X-API-Key`, siempre sobre HTTPS.

Su limitación es que todos comparten la misma llave, no expira y no dice quién
llamó. Para staging alcanza; para producción no.

**Producción: Cognito con client credentials, validado en el ALB.**

```
consumidor ──1. client_id + secreto──> Cognito /oauth2/token
           <─2. token (1 hora, scope nelua-api/read)
           ──3. Authorization: Bearer <token>──> WAF ─> ALB ─> pods
                                                        │
                                  4. valida firma, emisor, expiración
                                     y scope con las llaves públicas
                                     de Cognito (JWKS) antes de reenviar
```

- **Un cliente por consumidor** (`api_consumers` en `envs/prod.tfvars`). Si una
  credencial se filtra, se revoca solo esa, sin afectar a los demás, y se sabe de
  quién era.
- **Tokens de una hora.** Un token robado deja de servir solo. El secreto del
  cliente no viaja en cada petición, solo cuando pide un token nuevo.
- **La validación la hace el ALB**, con la acción `jwt-validation` (disponible
  desde noviembre de 2025). Un token inválido se rechaza antes de llegar a los
  pods: el tráfico sin credenciales no consume capacidad de la API, y la
  aplicación no cambia (en modo `jwt` deja de pedir la API key). Los pods solo
  aceptan tráfico que viene del ALB, por el security group.
- **Sin usuarios.** El pool solo tiene clientes de máquina; nadie se puede
  registrar.
- **El pipeline tiene su propio cliente** (`pipeline-verify`). Sus credenciales
  están en Secrets Manager y el rol del pipeline solo puede leer esas. Con ellas
  pide un token para verificar cada despliegue.
- `/healthz` sigue siendo público (solo dice si el servicio vive y qué versión
  corre), igual que en staging.

Lo que cuesta: Cognito no cobra por cliente, pero sí por token emitido (unos
0,0023 USD cada uno, sin capa gratuita para máquina a máquina). Con tokens de una
hora, cada consumidor pide unos 720 al mes: menos de 2 USD.

Lo que queda pendiente:

- El secreto de cada cliente lo genera Cognito y Terraform lo guarda en su
  estado, que está en un bucket cifrado y privado. Si eso no fuera aceptable, los
  clientes se crearían fuera de Terraform.
- El WAF sigue limitando por IP. Puede contar por un encabezado en lugar de por
  IP (*custom keys*), pero con JWT el encabezado `Authorization` cambia cada hora,
  así que la cuota sería por token y no exactamente por cliente: el WAF no
  decodifica el token para leer el `client_id`. Una cuota exacta por cliente
  pediría API Gateway con planes de uso delante del ALB.
- Si se suma un consumidor humano (un tablero web), el mismo pool sirve con el
  flujo de código de autorización.

## 2. La arquitectura

Hay un diagrama por ambiente. Son el mismo diseño con distintos valores.

![Ambiente de staging](arquitectura-staging.png)

![Ambiente de producción](arquitectura-produccion.png)

| Requisito | Cómo se cumple |
|---|---|
| Exposición pública segura | Solo está abierto el puerto 443; el 80 no, porque es una API y no un sitio. TLS 1.2 o superior con certificado de ACM. WAF con límite por IP y reglas administradas de AWS. Los pods no tienen IP pública y solo aceptan tráfico del ALB. Las peticiones exigen credenciales: API key en staging y un token de Cognito validado por el ALB en producción. `/metrics` no se entrega desde internet, lo lee Prometheus dentro del clúster. |
| Alta disponibilidad | Tres zonas. En producción hay mínimo tres réplicas repartidas entre ellas, un PodDisruptionBudget y despliegue gradual con `maxUnavailable: 0`. |
| Escalabilidad a 10.000 RPS | La API no tiene estado y responde desde memoria. El HPA escala por CPU, rápido hacia arriba y lento hacia abajo. EKS Auto Mode agrega nodos cuando hay pods que no caben. La carga hacia GitHub, Kubernetes y DynamoDB no crece con el tráfico. |
| Tolerancia a fallos | Probes de arranque, readiness y liveness. Apagado ordenado: el pod sigue atendiendo mientras el ALB lo retira. La foto en memoria aísla de fallos de las fuentes y de DynamoDB. DynamoDB replica en tres zonas y tiene respaldo continuo. Nodos Spot con On-Demand de respaldo. |
| Manejo de secretos | Las API keys, las credenciales del cliente de Cognito del pipeline y el token de GitHub están en Secrets Manager. Su valor no pasa por el repositorio, el pipeline ni el estado de Terraform. No hay llaves de AWS guardadas: GitHub entra por OIDC y los pods por Pod Identity, con un rol distinto para la API y para el recolector. |
| Monitoreo | SLOs de disponibilidad, latencia y frescura con alarmas de CloudWatch por consumo del presupuesto de error (`slo.tf`) y un tablero por SLO. Alarmas de diagnóstico (pods fuera de servicio, recolector sin sincronizar) que abren ticket sin despertar a nadie. Prometheus y Grafana en el clúster. Trazas con OpenTelemetry hacia X-Ray, con el mismo `trace_id` en los logs. Cada respuesta trae `meta.stale` y `meta.sync` con el motivo. |

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

Hay otro supuesto en el WAF. La API está abierta a cualquier IP: lo que protege
los datos es la credencial, no la dirección. El WAF solo agrega un límite contra
abuso de 2.000 peticiones por IP cada 5 minutos, que da por hecho que los 10.000
RPS vienen de muchos clientes. Si vinieran de pocos consumidores internos detrás
de las mismas IP, ese límite los bloquearía. Hay tres salidas, ninguna es
registrar IP por IP:

- Contar por consumidor en vez de por IP: las reglas de límite del WAF admiten
  *custom keys*, y con el encabezado `X-API-Key` como llave cada consumidor tiene
  su cuota sin importar desde dónde llama.
- Eximir los rangos de salida conocidos (CIDR, por ejemplo la red de la oficina)
  con un IP set.
- Subir el umbral (`waf_rate_limit`).

La prueba de carga usa la segunda: con `load_test_mode` se eximen las IP del NAT,
porque los generadores corren en el clúster y salen todos por la misma dirección.
Es temporal; al terminar la prueba se apaga.

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
| Autenticación | API key (`X-API-Key`) | Cognito, máquina a máquina: un cliente por consumidor, tokens de una hora validados en el ALB |
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
| Límite del WAF | Por IP | Por consumidor: *custom key* sobre `X-API-Key` en el WAF, o API Gateway con planes de uso si hace falta una cuota exacta por cliente de Cognito |
| Alertas | Por umbral, en CloudWatch | Por consumo del presupuesto de error |
| Cifrado | Llaves administradas por AWS | Llaves KMS propias donde haya requisito de cumplimiento |

### Alternativas que consideré

| Decisión | Elegí | Alternativa | Por qué |
|---|---|---|---|
| Cómputo | EKS Auto Mode | ECS Fargate | Fargate da menos trabajo y para un solo servicio habría sido una elección válida. Me fui por EKS porque se parece a lo que corre en producción en la mayoría de equipos de plataforma, y porque la API expone el estado de un clúster. Auto Mode se encarga de nodos, parches y escalado, que es lo más pesado de operar en Kubernetes. |
| Entrada | ALB con WAF | API Gateway | Con el cómputo en EKS, API Gateway no reemplaza al balanceador. Necesita uno detrás (VPC Link), así que sería una capa más. Cobra por petición, y a 10.000 RPS, aunque sea pocas horas al día, son miles de millones de peticiones al mes: sale órdenes de magnitud más caro que un ALB. Su cuota por defecto es de 10.000 RPS, el mismo número del pico. Pierdo cuotas por consumidor y autenticación en el borde. Si eso fuera un requisito, lo pondría delante del ALB. |
| Entrada | ALB con WAF | CloudFront delante | Las respuestas sí se podrían cachear unos segundos, pero la API ya responde desde memoria: sería una caché delante de otra caché. Los consumidores son internos, CloudFront cobra por petición y obliga a incluir la credencial en la llave de caché. Lo agregaría si la prueba de carga muestra que los pods no alcanzan. |
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

La única diferencia de comportamiento es la autenticación: API key en staging y
Cognito en producción. Es una concesión consciente, y tiene un costo: la validación
del token en el ALB no se prueba antes de producción. Cerrarla es una línea
(`auth_mode = "jwt"` en `envs/staging.tfvars`); el pipeline ya sabe pedir el token
en los dos modos. La dejé así porque staging es donde pruebo a mano y corro la
prueba de carga, y con API key es más simple.

Los secretos están separados. Cada ambiente tiene su credencial (API key en
staging, clientes de Cognito en producción), su tabla de DynamoDB
y sus roles de IAM, y un pod de staging no puede leer nada de producción. El token
de GitHub sí es uno solo, porque es de solo lectura y no da acceso a AWS.

Esta API no guarda datos de clientes, así que no hay nada sensible que copiar. En
un sistema con base de datos, staging trabajaría con datos sintéticos o
anonimizados generados por un job, nunca con una copia de producción.

Lo que falta para que el aislamiento sea completo es separar las cuentas de AWS.
Hoy los dos ambientes están en la misma.

### SLOs, SLIs y alertas

| SLI | Cómo se mide | SLO (30 días) | Presupuesto de error |
|---|---|---|---|
| Disponibilidad | Peticiones sin 5xx (del ALB o de los pods) sobre el total, medido en el ALB | 99,9 % | 0,1 %: unos 43 minutos de caída total al mes |
| Latencia | Peticiones que responden en menos de 300 ms sobre el total, en el ALB | 99 % | 1 % de peticiones lentas |
| Frescura | Sincronizaciones buenas del recolector sobre el total (`SyncSuccess`), la peor entre GitHub y el clúster | 99 % | 1 % de sincronizaciones fallidas |

Mido en el ALB porque ahí se ve lo que le pasa al cliente, incluso cuando no hay
ningún pod respondiendo. La frescura la agregué por cómo está hecha la API: si
responde 200 con datos de hace una hora, para quien la consulta está caída.

La latencia la definí como porcentaje de peticiones por debajo de un umbral y no
como un p95, porque así tiene presupuesto de error igual que las otras dos. En la
prueba de carga el p99 fue de unos 10 ms, así que 300 ms deja margen para las
dependencias que hoy no hay en el camino de la petición.

Las alertas son por consumo del presupuesto de error (burn rate) y no por
umbrales. Un burn rate de 1 gasta el presupuesto justo en los 30 días; uno de 14,4
lo gasta en dos días.

| Nivel | Condición | Ejemplo en disponibilidad | Acción |
|---|---|---|---|
| Rápido | 14,4x en 1 h y también en los últimos 5 min | Más de 1,44 % de 5xx | Despierta a alguien |
| Lento | 6x en 6 h y también en los últimos 30 min | Más de 0,6 % de 5xx | Despierta a alguien |
| Ticket | 1x tres días seguidos | Más de 0,1 % de 5xx | Ticket para el siguiente día hábil |

La ventana larga confirma que el problema es real y no un pico; la corta, que sigue
pasando. Sin la corta, la alarma seguiría sonando casi una hora después de
arreglar el problema. En CloudWatch cada ventana es una alarma con metric math y
cada nivel es una alarma compuesta (`ALARM(larga) AND ALARM(corta)`) que publica
en el topic de alertas. El ticket va a otro topic.

Con esto se evitan los dos problemas de las alarmas por umbral: un pod que se
reinicia y da 30 segundos de errores no despierta a nadie porque el presupuesto lo
absorbe, y un 0,3 % de errores constante, que ninguna alarma de umbral nota, abre
un ticket antes de que se coma el mes.

Con una sola persona de guardia, la regla es que solo la despierte algo que afecta
al usuario y que hay que atender ya. Por eso las alarmas de causa (pods fuera de
servicio en el ALB, recolector sin sincronizar con GitHub o con el clúster)
siguen existiendo pero abren ticket: dicen dónde mirar, no que haya que levantarse.

Los objetivos están en la variable `slo` del stack y las alarmas se calculan a
partir de ellos. El tablero `nelua-api-<ambiente>-slo` muestra por SLI el
cumplimiento de los últimos 30 días, cuánto presupuesto queda y la serie de
eventos malos por hora con las líneas de cada alarma. El presupuesto también sirve
para decidir: si queda, se puede desplegar con más riesgo; si se acabó, se
congelan los cambios que no sean de confiabilidad.

### Trazas distribuidas

Las trazas son con OpenTelemetry y van a AWS X-Ray. Lo que más me importaba
mostrar está en el recolector, que es el único que habla con sistemas externos:
cada ciclo de sincronización es una traza (`sync github`, `sync cluster`,
`sync budget`) con un span por vista (`collect deploys#owner/repo`) y, dentro, las
llamadas a GitHub, a la API de Kubernetes, a DynamoDB y a CloudWatch. Cuando un
repo falla, su span queda en error con la excepción, y el ciclo también. Si GitHub
se pone lento o DynamoDB empieza a reintentar, se ve en qué llamada se fue el tiempo.

En la API cada petición es un span con su ruta. La traza tiene un solo span porque
la petición responde desde memoria; eso es justamente lo que buscaba el diseño. La
relectura de DynamoDB cada 5 s también es una traza (`refresh snapshots`). Las
rutas de operación (`/healthz`, `/readyz`, `/metrics`) no generan trazas.

Cómo está armado:

- **La app no sabe a dónde van las trazas.** Las manda por OTLP a un colector de
  OpenTelemetry dentro del clúster (`iac/k8s/tracing/otel-collector.yaml`, dos
  réplicas), y es él quien las envía a X-Ray con su propio rol de IAM, que solo
  puede escribir trazas. Cambiar X-Ray por Tempo o Jaeger es cambiar ese archivo,
  no el código.
- **Instrumentación automática más spans propios.** FastAPI, httpx y boto3 se
  instrumentan solos; los spans de sincronización los agregué a mano porque son
  los que dan sentido a la traza.
- **Configuración estándar.** La app lee las variables `OTEL_*` del SDK y las pone
  el chart. Sin `OTEL_EXPORTER_OTLP_ENDPOINT` las trazas quedan apagadas y el
  código de spans no cuesta nada, así que en local y en las pruebas todo funciona igual.
- **Si el colector se cae, la app sigue.** El envío es en lote y en otro hilo; si
  no hay a dónde mandar, las trazas se descartan y la petición no espera.
- **Logs con el mismo identificador.** Cada línea de log lleva `trace_id=`, que es
  el mismo de X-Ray (X-Ray lo muestra como `1-<8 primeros>-<resto>`). De un error en
  los logs se llega a su traza y al revés. Las peticiones guardan además el
  `X-Amzn-Trace-Id` que agrega el ALB, para cruzarlas con sus logs.

**El muestreo lo decidí por costo.** X-Ray cobra por traza guardada (del orden de
5 USD por millón). A 10.000 RPS, guardar todas serían cientos de millones al día.
Por eso en producción la API guarda el 0,1 % (unas 10 trazas por segundo en el
pico), mientras que el recolector guarda todas, porque son unos 6 ciclos por
minuto. En staging se guarda todo porque el tráfico es el de las pruebas, salvo
durante la prueba de carga, que muestrea como producción. La decisión se toma al
inicio de cada traza y se respeta en toda ella, así que nunca quedan trazas a medias.

Lo probé en local sin AWS: la API y el recolector mandaron sus trazas a un colector
real con la misma configuración del clúster, apuntando a un X-Ray simulado, y
revisé los documentos que habría recibido X-Ray: el ciclo con sus hijos, el error
del repo que falla y el `trace_id` de los logs igual al de la traza. En AWS falta
verlas en la consola la próxima vez que se levante staging.

Lo siguiente sería muestreo por cola en el colector (guardar todas las trazas con
error o lentas y una fracción del resto), que exige mandar todos los spans al
colector y por eso más capacidad en él.

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

El gasto también se consulta en la misma API, en `/v1/budget`. Terraform crea un
presupuesto mensual (`monthly_budget_usd`) y, si se configura un correo, AWS avisa
al 80 % del gasto y cuando el pronóstico del mes supera el límite.

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

- Cognito también en staging, para que la autenticación sea idéntica en los dos
  ambientes. Hoy staging usa API key para que probar sea más simple.
- Muestreo por cola de las trazas (todas las que tienen error o son lentas).
- Despliegue canary con Argo Rollouts. Hoy el rollback es rápido, pero una versión
  mala alcanza a recibir todo el tráfico antes de que se detecte.
- Un rol de IAM de solo lectura para el `plan` de Terraform en pull requests, y un
  *permission boundary* para el rol que aplica.
- Una cuenta de AWS por ambiente.
- Access logs del ALB con muestreo.
- Recibir los despliegues de GitHub por webhook en lugar de consultarlos cada
  minuto: datos al instante y casi sin consumo del límite.
