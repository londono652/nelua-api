# API keys de la API, una por entorno: staging nunca comparte secreto con prod.
#
# El valor se genera aquí pero NO queda en el estado de Terraform ni en el
# repositorio: se usa un recurso efímero y un atributo de solo escritura
# (write-only). El único lugar donde vive la llave es Secrets Manager.
locals {
  environments = toset(["staging", "prod"])
}

ephemeral "aws_secretsmanager_random_password" "api_key" {
  for_each = local.environments

  password_length     = 40
  exclude_punctuation = true
}

resource "aws_secretsmanager_secret" "api_keys" {
  #checkov:skip=CKV_AWS_149:El secreto ya se cifra en reposo con la llave administrada por AWS. Una llave KMS propia agrega costo mensual y gestion de politicas sin beneficio para este alcance.
  #checkov:skip=CKV2_AWS_57:La rotacion automatica exige una funcion Lambda. Aqui la rotacion es un cambio de version en Terraform (api_key_version) y la API recarga el secreto cada 5 minutos, sin redesplegar.
  for_each = local.environments

  name        = "${var.project}/${each.key}/api-keys"
  description = "API keys validas para ${var.project} en ${each.key} (lista JSON)"

  # Permite recrear el secreto con el mismo nombre sin esperar la ventana de borrado.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "api_keys" {
  for_each = local.environments

  secret_id = aws_secretsmanager_secret.api_keys[each.key].id

  # Lista JSON: permite tener dos llaves válidas a la vez durante una rotación.
  secret_string_wo = jsonencode([ephemeral.aws_secretsmanager_random_password.api_key[each.key].random_password])

  # Para rotar la llave: subir este número y aplicar.
  secret_string_wo_version = var.api_key_version
}

# Token de GitHub con el que el recolector lee los despliegues. Es uno solo para
# los dos ambientes y es de solo lectura (fine-grained, limitado a los repos
# monitoreados; para repos públicos no necesita permisos adicionales y para
# privados basta "Deployments: Read"). No da acceso a nada de AWS.
#
# Para repos públicos funciona sin token, pero el límite baja de 5.000 a 60
# peticiones por hora, compartidas por todo el clúster (sale por la IP del NAT).
#
# Terraform crea el secreto VACÍO: el token lo genera una persona en GitHub y lo
# guarda directo en Secrets Manager, así nunca pasa por el repositorio, el estado
# de Terraform ni los logs del pipeline. Ver run.md.
resource "aws_secretsmanager_secret" "github_token" {
  #checkov:skip=CKV_AWS_149:Cifrado con la llave administrada por AWS, igual que las API keys.
  #checkov:skip=CKV2_AWS_57:El token lo emite GitHub; se rota generando uno nuevo y guardándolo con put-secret-value. El recolector lo lee al arrancar.
  name        = "${var.project}/github-token"
  description = "Token de GitHub de solo lectura para el recolector de ${var.project}"

  recovery_window_in_days = 0
}
