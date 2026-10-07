# Identidad de la API dentro de AWS (EKS Pod Identity).
#
# El pod no tiene llaves de AWS: EKS le entrega credenciales temporales del rol
# asociado a su cuenta de servicio. Hay un rol por entorno, así el pod de
# staging no puede leer el secreto de prod.
data "aws_iam_policy_document" "api_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  for_each = local.environments

  name               = "${var.project}-${each.key}"
  description        = "Rol de los pods de ${var.project} en ${each.key}"
  assume_role_policy = data.aws_iam_policy_document.api_trust.json
}

# Solo lectura y solo lo que la API usa: los presupuestos de la cuenta y SU secreto.
data "aws_iam_policy_document" "api_permissions" {
  for_each = local.environments

  statement {
    sid       = "ReadBudgets"
    actions   = ["budgets:ViewBudget"]
    resources = ["arn:aws:budgets::${local.account_id}:budget/*"]
  }

  statement {
    sid       = "ReadOwnApiKeys"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.project}/${each.key}/api-keys-*"]
  }
}

resource "aws_iam_role_policy" "api" {
  for_each = local.environments

  name   = "read-only"
  role   = aws_iam_role.api[each.key].id
  policy = data.aws_iam_policy_document.api_permissions[each.key].json
}

# Enlaza el rol con la cuenta de servicio "nelua-api" del namespace del entorno.
resource "aws_eks_pod_identity_association" "api" {
  for_each = local.environments

  cluster_name    = module.eks.cluster_name
  namespace       = each.key
  service_account = var.project
  role_arn        = aws_iam_role.api[each.key].arn
}
