# Identidades de la aplicación dentro de AWS (EKS Pod Identity).
#
# Los pods no tienen llaves de AWS: EKS les entrega credenciales temporales del
# rol asociado a su cuenta de servicio. Hay dos roles por ambiente, uno por
# componente, cada uno con lo mínimo que necesita:
#
#   API         lee las fotos de DynamoDB y el secreto de SUS API keys.
#   recolector  escribe en DynamoDB, lee el token de GitHub y los presupuestos
#               de la cuenta, y publica en CloudWatch si cada sincronización
#               salió bien.
#
# Así, un pod de la API comprometido no puede escribir datos ni leer el token
# de GitHub, y el de staging no puede leer nada de producción.
data "aws_iam_policy_document" "pod_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# ---------- API ----------

resource "aws_iam_role" "api" {
  name               = local.name
  description        = "Rol de los pods de la API de ${var.project} en ${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

data "aws_iam_policy_document" "api_permissions" {
  statement {
    sid       = "ReadSnapshots"
    actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
    resources = [aws_dynamodb_table.api.arn]
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

resource "aws_eks_pod_identity_association" "api" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.app_namespace
  service_account = var.project
  role_arn        = aws_iam_role.api.arn
}

# ---------- Recolector ----------

resource "aws_iam_role" "collector" {
  name               = "${local.name}-collector"
  description        = "Rol del recolector de ${var.project} en ${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

data "aws_iam_policy_document" "collector_permissions" {
  #checkov:skip=CKV_AWS_356:PutMetricData no admite restringir por recurso; se limita con la condicion cloudwatch:namespace.
  statement {
    sid = "WriteHistoryAndSnapshots"
    actions = [
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable",
      # Al arrancar lee el estado de la última sincronización (restore_sync_status).
      "dynamodb:BatchGetItem",
    ]
    resources = [aws_dynamodb_table.api.arn]
  }

  # Presupuestos de la cuenta, para GET /v1/budget. Solo lectura.
  statement {
    sid       = "ReadBudgets"
    actions   = ["budgets:ViewBudget"]
    resources = ["arn:aws:budgets::${local.account_id}:budget/*"]
  }

  # Solo puede publicar métricas en el namespace de la aplicación.
  statement {
    sid       = "PublishSyncMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.project]
    }
  }

  statement {
    sid       = "ReadGitHubToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.project}/github-token-*"]
  }
}

resource "aws_iam_role_policy" "collector" {
  name   = "collector"
  role   = aws_iam_role.collector.id
  policy = data.aws_iam_policy_document.collector_permissions.json
}

resource "aws_eks_pod_identity_association" "collector" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.app_namespace
  service_account = "${var.project}-collector"
  role_arn        = aws_iam_role.collector.arn
}
