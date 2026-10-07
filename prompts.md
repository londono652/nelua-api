# Uso de IA en el reto

Usé un asistente de IA (Claude) como par de trabajo durante todo el reto. Este
documento cuenta cómo, sin maquillarlo: qué delegué, qué decidí yo y dónde la IA
se equivocó.

## Cómo la usé

| Para qué | Ejemplo |
|---|---|
| **Contrastar la arquitectura** | Pedí una propuesta y luego la cuestioné punto por punto hasta dejar solo lo que podía explicar. |
| **Escribir código y pruebas** | La API, las pruebas, el chart y Terraform se escribieron con IA; yo ejecuté, probé y validé cada entrega en mi máquina y en los pipelines. |
| **Depurar** | Le pasaba la salida real del error (un pipeline, un `docker run`, un `terraform plan`) y trabajábamos sobre eso. |
| **Preparar la sustentación** | Preguntas difíciles que me podrían hacer y cómo responderlas. |

## Prompts representativos

Planteamiento inicial:

> "Supón que eres un arquitecto DevOps que quiere desplegar una API en Python, con
> un flujo de CI/CD usando GitHub Actions, Terraform y AWS. Quieres hacer todo
> funcional (la API, el pipeline y la IaC) cumpliendo a cabalidad cada punto.
> ¿Cómo lo elaborarías de forma que se pueda explicar fácilmente, qué arquitectura
> elegirías y cómo separarías el flujo de CI/CD de la IaC?"

Revisión crítica de lo propuesto:

> "Analiza si está bien la arquitectura propuesta para este reto y, si hay algo por
> cambiar y mejorar, proponlo. La idea es algo simple para poder sustentarlo sin
> complicarme."

Sobre el dominio de la API:

> "¿No podríamos hacer algo más sencillo, por ejemplo que obtenga el estado del
> presupuesto de AWS y el estado de los deployments del mismo EKS? No me suena
> obtener información y meterle data dummy; ahí mismo dice que es para un equipo
> de infra."

Sobre el pipeline:

> "El rollback sí debe estar como paso en el pipeline y mostrar bien la estrategia."
>
> "Quisiera que los pipelines los agrupes por etapas de CI/CD; el requerimiento pide
> etapas bien definidas."

## Decisiones en las que corregí a la IA

La IA tiende a proponer de más. Buena parte de mi trabajo fue recortar:

- **Propuso Redis y DynamoDB a la vez.** Pregunté para qué ambos; no había una
  buena razón. Al final la API no tiene base de datos.
- **Propuso redirigir HTTP a HTTPS.** Para una API no tiene sentido abrir el puerto
  80; quedó solo HTTPS.
- **Propuso un registro de servicios con datos de ejemplo.** Lo rechacé: una API
  para un equipo de infraestructura debe mostrar datos reales.
- **Llegó a ocho endpoints.** Pedí dejar máximo cuatro que cubrieran lo mismo.
- **Propuso JWT con Cognito.** Es mejor que una API key, pero complicaba la
  presentación sin cambiar lo que se evalúa; quedó documentado como evolución.
- **Quise separar** el pipeline de infraestructura del de la aplicación, y tener
  una forma de apagar la plataforma para no gastar: fueron requisitos míos.

## Dónde se equivocó y cómo se detectó

Nada de esto se habría visto sin ejecutar las cosas de verdad:

- **OIDC con GitHub:** la confianza de IAM usaba el formato clásico del *subject*.
  El pipeline falló con `Not authorized to perform sts:AssumeRoleWithWebIdentity`;
  los repositorios nuevos usan identificadores inmutables y hubo que ajustarlo.
- **Imagen de Docker:** el contenedor no arrancaba porque los archivos quedaron con
  permisos que el usuario no-root no podía leer.
- **checkov:** en local pasaba y en CI fallaba; eran versiones distintas de la
  herramienta. Se igualó usando la misma imagen en ambos lados.
- **Vulnerabilidad en la imagen base** detectada por Trivy en el pipeline.

## Lo que no delegué

- La decisión de qué construir y qué dejar fuera.
- La verificación: todo lo que está en este repositorio lo corrí yo.
- Las credenciales: la IA nunca tuvo llaves de AWS ni de GitHub.

## Lo que me llevo

La IA acelera mucho la escritura y la exploración de alternativas, pero no
reemplaza el criterio: sus primeras propuestas eran más complejas de lo necesario
y varios errores solo aparecieron al desplegar. Lo que hizo la diferencia fue
exigirle simplicidad, pedirle el porqué de cada pieza y no dar nada por bueno
hasta verlo funcionar.
