# GitHub Actions se autentica en AWS con OIDC: no hay llaves guardadas en GitHub.
# El repositorio tiene dos roles, uno por pipeline, y cada uno solo se puede
# asumir desde contextos concretos (rama main, entornos con aprobación o PR).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  # Formato del "subject" que GitHub pone en el token OIDC. Los repositorios
  # creados desde julio de 2026 usan identificadores inmutables:
  #   repo:<usuario>@<id-usuario>/<repo>@<id-repo>:<contexto>
  # Los IDs numéricos no cambian aunque el usuario o el repo se renombren, así
  # que nadie puede suplantar al repo registrando después el mismo nombre.
  repo_subject = "repo:${var.github_owner}@${var.github_owner_id}/${var.repo}@${var.repo_id}"

  # Pipeline de IaC: plan en pull requests y en main; apply solo desde el
  # entorno "infra", que exige aprobación manual.
  infra_subjects = [
    "${local.repo_subject}:ref:refs/heads/main",
    "${local.repo_subject}:environment:infra",
    "${local.repo_subject}:pull_request",
  ]

  # Pipeline de la app: publicar la imagen desde main y desplegar desde los
  # entornos "staging" y "prod" (este último con aprobación manual).
  app_subjects = [
    "${local.repo_subject}:ref:refs/heads/main",
    "${local.repo_subject}:environment:staging",
    "${local.repo_subject}:environment:prod",
  ]
}

# ---------- Rol del pipeline de infraestructura ----------
data "aws_iam_policy_document" "infra_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.infra_subjects
    }
  }
}

resource "aws_iam_role" "gha_infra" {
  name               = "${var.project}-gha-infra"
  description        = "Rol que asume el pipeline de IaC del repo ${var.repo}"
  assume_role_policy = data.aws_iam_policy_document.infra_trust.json
}

# Decisión consciente: el pipeline de IaC crea VPC, EKS, IAM, ALB, WAF y DNS,
# así que necesita permisos amplios. El control está en la confianza: solo este
# repositorio puede asumir el rol, y el apply solo corre tras una aprobación
# manual. En producción se acotaría además con un permission boundary.
resource "aws_iam_role_policy_attachment" "gha_infra_admin" {
  #checkov:skip=CKV_AWS_274:El pipeline de IaC crea IAM, VPC, EKS, ALB, WAF y DNS. El control esta en la confianza OIDC (solo este repositorio, con IDs inmutables, y apply con aprobacion manual). En produccion se acotaria con un permission boundary.
  role       = aws_iam_role.gha_infra.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ---------- Rol del pipeline de la aplicación ----------
data "aws_iam_policy_document" "app_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.app_subjects
    }
  }
}

resource "aws_iam_role" "gha_app" {
  name               = "${var.project}-gha-app"
  description        = "Rol que asume el pipeline de la app del repo ${var.repo}"
  assume_role_policy = data.aws_iam_policy_document.app_trust.json
}

# Mínimo privilegio: subir imágenes a SU repositorio de ECR, leer el contrato de
# infraestructura en Parameter Store, conectarse al clúster y leer la API key
# para verificar el despliegue. No crea infraestructura.
data "aws_iam_policy_document" "app_permissions" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.project}"]
  }

  statement {
    sid = "ReadInfraContract"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.project}/*"]
  }

  # Credenciales con las que el pipeline verifica cada despliegue: la API key
  # (staging) o el cliente de Cognito de verificación (producción, modo jwt).
  statement {
    sid     = "ReadCredentialsForSmokeTests"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.project}/*/api-keys-*",
      "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.project}/*/oauth-client-verify-*",
    ]
  }

  statement {
    sid       = "DescribeCluster"
    actions   = ["eks:DescribeCluster"]
    resources = ["arn:aws:eks:${var.region}:${local.account_id}:cluster/${var.project}*"]
  }
}

resource "aws_iam_role_policy" "gha_app" {
  name   = "deploy"
  role   = aws_iam_role.gha_app.id
  policy = data.aws_iam_policy_document.app_permissions.json
}
