# Contrato con el pipeline de la aplicación, por ambiente:
#   /nelua-api/<ambiente>/eks/cluster-name
#   /nelua-api/<ambiente>/alb/target-group-arn
locals {
  parameters = {
    "eks/cluster-name"     = module.eks.cluster_name
    "alb/target-group-arn" = aws_lb_target_group.api.arn
  }
}

resource "aws_ssm_parameter" "contract" {
  #checkov:skip=CKV2_AWS_34:Estos parametros no son secretos (nombres, ARNs y dominios). Los secretos viven en Secrets Manager; cifrarlos obligaria a dar permisos de KMS al pipeline sin proteger nada.
  for_each = local.parameters

  name  = "/${var.project}/${var.environment}/${each.key}"
  type  = "String"
  value = each.value
}
