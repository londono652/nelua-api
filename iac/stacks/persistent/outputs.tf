output "ecr_repository_url" {
  description = "URL del repositorio de imágenes"
  value       = aws_ecr_repository.api.repository_url
}

output "certificate_arn" {
  description = "ARN del certificado validado"
  value       = aws_acm_certificate_validation.api.certificate_arn
}

output "hostnames" {
  description = "Nombres públicos de la API"
  value = {
    prod    = "api.${var.domain}"
    staging = "api-staging.${var.domain}"
  }
}

output "api_keys_secrets" {
  description = "Nombres de los secretos con las API keys (el valor no se muestra)"
  value       = { for env, secret in aws_secretsmanager_secret.api_keys : env => secret.name }
}

output "github_token_secret" {
  description = "Secreto donde se guarda a mano el token de GitHub del recolector"
  value       = aws_secretsmanager_secret.github_token.name
}

output "budget_name" {
  description = "Presupuesto mensual de la cuenta (lo expone la API en /v1/budget)"
  value       = aws_budgets_budget.monthly.name
}
