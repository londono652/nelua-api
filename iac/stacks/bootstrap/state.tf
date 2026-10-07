# Bucket S3 donde los stacks persistent y platform guardan su estado remoto.
data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "${var.project}-tfstate-${local.account_id}"
}

resource "aws_s3_bucket" "tfstate" {
  #checkov:skip=CKV_AWS_145:El estado ya se cifra en reposo con AES-256 administrado por AWS; una llave KMS propia agrega costo y gestion sin beneficio para este alcance.
  #checkov:skip=CKV_AWS_18:Los access logs exigirian un segundo bucket solo para auditar el del estado; en produccion se cubriria con CloudTrail data events.
  #checkov:skip=CKV_AWS_144:El versionado ya permite recuperar cualquier estado anterior; la replicacion entre regiones es para recuperacion ante desastres regionales, fuera del alcance.
  #checkov:skip=CKV2_AWS_62:Nadie necesita reaccionar a eventos del bucket del estado.
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

# Versionado: permite recuperar un estado anterior si algo se corrompe.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Costo y orden: las versiones antiguas del estado se borran a los 90 días.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.tfstate]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Rechaza cualquier acceso al bucket que no use TLS.
data "aws_iam_policy_document" "tfstate_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}
