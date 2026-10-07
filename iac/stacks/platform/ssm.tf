# Contrato con el pipeline de la aplicación.
locals {
  parameters = merge(
    {
      "eks/cluster-name" = module.eks.cluster_name
    },
    {
      for env, target_group in aws_lb_target_group.api :
      "alb/target-group-arn-${env}" => target_group.arn
    },
  )
}

resource "aws_ssm_parameter" "contract" {
  #checkov:skip=CKV2_AWS_34:Estos parametros no son secretos (nombres, ARNs y dominios). Los secretos irian en Secrets Manager; cifrarlos obligaria a dar permisos de KMS al pipeline sin proteger nada.
  for_each = local.parameters

  name  = "/${var.project}/${each.key}"
  type  = "String"
  value = each.value
}
