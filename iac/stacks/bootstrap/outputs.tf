output "state_bucket" {
  description = "Bucket para el backend S3 de los demás stacks"
  value       = aws_s3_bucket.tfstate.bucket
}

output "gha_infra_role_arn" {
  description = "ARN del rol para el pipeline de IaC (variable AWS_INFRA_ROLE_ARN del repo)"
  value       = aws_iam_role.gha_infra.arn
}

output "gha_app_role_arn" {
  description = "ARN del rol para el pipeline de la app (variable AWS_APP_ROLE_ARN del repo)"
  value       = aws_iam_role.gha_app.arn
}

output "hosted_zone_id" {
  description = "ID de la zona de Route 53"
  value       = aws_route53_zone.main.zone_id
}

output "name_servers" {
  description = "Pega estos 4 name servers en GoDaddy (Nameservers personalizados)"
  value       = aws_route53_zone.main.name_servers
}
