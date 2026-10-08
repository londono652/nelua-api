# Identidad de la API dentro de AWS (EKS Pod Identity).
#
# El pod no tiene llaves de AWS: EKS le entrega credenciales temporales del rol
# asociado a su cuenta de servicio. Cada ambiente tiene su rol, así que el pod
# de staging no puede leer el secreto de producción.
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
  name               = local.name
  description        = "Rol de los pods de ${var.project} en ${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.api_trust.json
}

# Solo lectura y solo lo que la API usa: los presupuestos de la cuenta y el
# secreto de SU ambiente.
data "aws_iam_policy_document" "api_permissions" {
  statement {
    sid       = "ReadBudgets"
    actions   = ["budgets:ViewBudget"]
    resources = ["arn:aws:budgets::${local.account_id}:budget/*"]
  }

  statement {
    sid       = "ReadOwnApiKeys"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.project}/${var.environment}/api-keys-*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "read-only"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_permissions.json
}

# Enlaza el rol con la cuenta de servicio de la API en su namespace.
resource "aws_eks_pod_identity_association" "api" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.app_namespace
  service_account = var.project
  role_arn        = aws_iam_role.api.arn
}
