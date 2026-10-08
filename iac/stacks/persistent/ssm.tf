# Contrato entre infraestructura y aplicación: lo que este stack crea se publica
# en Parameter Store, y el pipeline de la app lo lee con permisos de solo lectura.
locals {
  parameters = merge(
    {
      "ecr/repository-url"   = aws_ecr_repository.api.repository_url
      "acm/certificate-arn"  = aws_acm_certificate_validation.api.certificate_arn
      "dns/zone-id"          = data.aws_route53_zone.main.zone_id
      "dns/hostname-prod"    = "api.${var.domain}"
      "dns/hostname-staging" = "api-staging.${var.domain}"
    },
    # Solo el NOMBRE del secreto: el valor nunca pasa por Parameter Store.
    {
      for env, secret in aws_secretsmanager_secret.api_keys :
      "secrets/api-keys-${env}" => secret.name
    },
    { "secrets/github-token" = aws_secretsmanager_secret.github_token.name },
  )
}

resource "aws_ssm_parameter" "contract" {
  #checkov:skip=CKV2_AWS_34:Estos parametros no son secretos (nombres, ARNs y dominios). Los secretos viven en Secrets Manager; cifrarlos obligaria a dar permisos de KMS al pipeline sin proteger nada.
  for_each = local.parameters

  name  = "/${var.project}/${each.key}"
  type  = "String"
  value = each.value
}
