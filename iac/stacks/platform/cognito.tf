# Autenticación de máquina a máquina con Cognito (solo con auth_mode = "jwt").
#
#   1. Cada consumidor tiene su propio cliente de Cognito (client_id + secreto).
#   2. Pide un token al endpoint /oauth2/token con el flujo client credentials.
#      El token dura una hora y trae el scope nelua-api/read.
#   3. Llama a la API con "Authorization: Bearer <token>".
#   4. El ALB valida la firma (con las llaves públicas de Cognito), el emisor, la
#      expiración y el scope ANTES de enviar la petición a los pods. Un token
#      inválido no llega a la aplicación.
#
# Frente a la API key: cada consumidor tiene su credencial (se revoca uno sin
# afectar a los demás), el token expira solo y el ALB rechaza el tráfico sin
# token antes de consumir capacidad de los pods.
locals {
  jwt_enabled = var.auth_mode == "jwt"
  # El pipeline usa su propio cliente para verificar cada despliegue.
  oauth_clients = local.jwt_enabled ? toset(concat(var.api_consumers, ["pipeline-verify"])) : toset([])
  oauth_scope   = "${var.project}/read"
  jwt_issuer    = local.jwt_enabled ? "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.api[0].id}" : ""
}

resource "aws_cognito_user_pool" "api" {
  count = local.jwt_enabled ? 1 : 0

  name = local.name

  # Sin usuarios: solo se usan clientes de máquina. Nadie se puede registrar.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # Recrear el pool cambia el emisor de los tokens y obliga a reconfigurar a
  # todos los consumidores: se protege igual que el balanceador.
  deletion_protection = var.alb_deletion_protection ? "ACTIVE" : "INACTIVE"
}

# El "recurso" que protege la API y el permiso que se otorga: nelua-api/read.
resource "aws_cognito_resource_server" "api" {
  count = local.jwt_enabled ? 1 : 0

  user_pool_id = aws_cognito_user_pool.api[0].id
  identifier   = var.project
  name         = var.project

  scope {
    scope_name        = "read"
    scope_description = "Lectura de despliegues y estado de los servicios"
  }
}

# Dominio del endpoint de tokens: https://<prefijo>.auth.<región>.amazoncognito.com
resource "aws_cognito_user_pool_domain" "api" {
  count = local.jwt_enabled ? 1 : 0

  domain       = "${local.name}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.api[0].id
}

resource "aws_cognito_user_pool_client" "consumer" {
  for_each = local.oauth_clients

  name         = each.key
  user_pool_id = aws_cognito_user_pool.api[0].id

  generate_secret                      = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = aws_cognito_resource_server.api[0].scope_identifiers
  supported_identity_providers         = ["COGNITO"]

  # Tokens de una hora: si uno se filtra, deja de servir solo.
  access_token_validity = 1
  token_validity_units {
    access_token = "hours"
  }
}

# Credenciales del cliente de verificación, para el pipeline de la aplicación.
# Las de los demás consumidores se entregan con
#   aws cognito-idp describe-user-pool-client --user-pool-id ... --client-id ...
# a quien tenga permiso de IAM para leerlas.
resource "aws_secretsmanager_secret" "oauth_verify" {
  #checkov:skip=CKV_AWS_149:Cifrado con la llave administrada por AWS, igual que las API keys.
  #checkov:skip=CKV2_AWS_57:El secreto lo emite Cognito; se rota recreando el cliente de verificacion con Terraform.
  count = local.jwt_enabled ? 1 : 0

  name                    = "${var.project}/${var.environment}/oauth-client-verify"
  description             = "Cliente de Cognito con el que el pipeline verifica cada despliegue de ${local.name}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "oauth_verify" {
  count = local.jwt_enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.oauth_verify[0].id
  secret_string = jsonencode({
    client_id     = aws_cognito_user_pool_client.consumer["pipeline-verify"].id
    client_secret = aws_cognito_user_pool_client.consumer["pipeline-verify"].client_secret
    token_url     = "https://${aws_cognito_user_pool_domain.api[0].domain}.auth.${var.region}.amazoncognito.com/oauth2/token"
    scope         = local.oauth_scope
  })
}
