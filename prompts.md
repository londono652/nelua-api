# Uso de IA en el reto

Trabajé todo el reto con un asistente de IA (Claude). Aquí cuento cómo lo usé, qué
decidí yo y en qué se equivocó.

## Para qué lo usé

Lo usé sobre todo para cuatro cosas.

La primera fue discutir la arquitectura. Le pedí una propuesta y después la fui
cuestionando punto por punto, hasta quedarme con lo que yo podía explicar.

La segunda fue escribir. El código de la API, las pruebas, el chart y Terraform
los escribió la IA. Yo corrí cada entrega en mi máquina y en los pipelines antes
de darla por buena.

La tercera, depurar. Le pegaba la salida real del error, ya fuera de un pipeline,
de un `docker run` o de un `terraform plan`, y trabajábamos sobre eso.

Y la cuarta, preparar la sustentación: qué me podían preguntar y cómo responderlo.

## Algunos prompts

Así arranqué:

> "Supón que eres un arquitecto DevOps que quiere desplegar una API en Python, con
> un flujo de CI/CD usando GitHub Actions, Terraform y AWS. Quieres hacer todo
> funcional (la API, el pipeline y la IaC) cumpliendo a cabalidad cada punto.
> ¿Cómo lo elaborarías de forma que se pueda explicar fácilmente, qué arquitectura
> elegirías y cómo separarías el flujo de CI/CD de la IaC?"

Para revisar lo que me había propuesto:

> "Analiza si está bien la arquitectura propuesta para este reto y, si hay algo por
> cambiar y mejorar, proponlo. La idea es algo simple para poder sustentarlo sin
> complicarme."

Sobre qué debía exponer la API:

> "¿No podríamos hacer algo más sencillo, por ejemplo que obtenga el estado del
> presupuesto de AWS y el estado de los deployments del mismo EKS? No me suena
> obtener información y meterle data dummy; ahí mismo dice que es para un equipo
> de infra."

Y sobre el pipeline:

> "El rollback sí debe estar como paso en el pipeline y mostrar bien la estrategia."
>
> "Quisiera que los pipelines los agrupes por etapas de CI/CD; el requerimiento pide
> etapas bien definidas."

## Dónde tuve que corregirla

La IA tiende a proponer de más, y buena parte de mi trabajo fue recortar.

Propuso Redis y DynamoDB al mismo tiempo. Le pregunté para qué los dos y no había
una buena razón. Al final la API no tiene base de datos.

Propuso redirigir HTTP a HTTPS. En una API no tiene sentido abrir el puerto 80, y
quedó solo HTTPS.

Propuso un registro de servicios con datos de ejemplo. Lo descarté porque una API
para un equipo de infraestructura debe mostrar datos reales.

Llegó a tener ocho endpoints. Le pedí dejar máximo cuatro que cubrieran lo mismo.

Propuso JWT con Cognito. Es mejor que una API key, pero complicaba la presentación
sin cambiar lo que se evalúa. Quedó anotado como siguiente paso.

Hubo también cosas que pedí yo desde el principio: que el pipeline de
infraestructura fuera independiente del de la aplicación, y poder apagar la
plataforma para no gastar.

## Dónde se equivocó

Ninguno de estos errores se habría visto sin ejecutar las cosas.

- La confianza OIDC en IAM usaba el formato clásico del *subject*. El pipeline
  falló con `Not authorized to perform sts:AssumeRoleWithWebIdentity`, porque los
  repositorios nuevos de GitHub usan identificadores inmutables. Hubo que ajustarlo.
- El contenedor no arrancaba. Los archivos habían quedado con permisos que el
  usuario no-root no podía leer.
- checkov pasaba en local y fallaba en CI. Eran versiones distintas de la
  herramienta, y lo resolvimos usando la misma imagen en los dos lados.
- Trivy encontró en el pipeline una vulnerabilidad en la imagen base.

## Lo que no delegué

Qué construir y qué dejar por fuera lo decidí yo. Todo lo que hay en este
repositorio lo corrí yo. Y la IA nunca tuvo llaves de AWS ni de GitHub.

## Lo que me llevo

La IA me ahorró mucho tiempo escribiendo y comparando alternativas. Pero sus
primeras propuestas eran más complejas de lo necesario, y varios errores solo
aparecieron al desplegar. Lo que mejor me funcionó fue pedirle que simplificara,
preguntarle el porqué de cada pieza y no dar nada por terminado hasta verlo correr.
