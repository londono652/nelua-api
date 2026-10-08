output "environment" {
  description = "Ambiente de este stack"
  value       = var.environment
}

output "cluster_name" {
  description = "Nombre del clúster EKS"
  value       = module.eks.cluster_name
}

output "kubeconfig_command" {
  description = "Comando para conectar kubectl al clúster"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.region}"
}

output "alb_dns_name" {
  description = "Nombre DNS del balanceador"
  value       = aws_lb.api.dns_name
}

output "url" {
  description = "URL pública de la API en este ambiente"
  value       = "https://${local.hostname}"
}

output "target_group_arn" {
  description = "Target group donde se registran los pods"
  value       = aws_lb_target_group.api.arn
}

output "waf_web_acl_arn" {
  description = "ARN de la web ACL del WAF asociada al ALB"
  value       = aws_wafv2_web_acl.api.arn
}

output "alerts_topic_arn" {
  description = "Topic de SNS que recibe las alarmas de CloudWatch"
  value       = aws_sns_topic.alerts.arn
}

output "dynamodb_table" {
  description = "Tabla de DynamoDB que comparten el recolector y la API"
  value       = aws_dynamodb_table.api.name
}
