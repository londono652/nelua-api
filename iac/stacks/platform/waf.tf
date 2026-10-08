# WAF asociado directamente al ALB.

locals {
  # IPs exentas del límite del WAF durante la prueba de carga: las indicadas a
  # mano y, en modo prueba, las IPs públicas del NAT (por donde sale el clúster).
  load_test_addresses = concat(
    var.load_test_cidrs,
    var.load_test_mode ? [for ip in module.vpc.nat_public_ips : "${ip}/32"] : [],
  )
  load_test_enabled = var.load_test_mode || length(var.load_test_cidrs) > 0
}

resource "aws_wafv2_ip_set" "load_test" {
  count = local.load_test_enabled ? 1 : 0

  name               = "${local.name}-load-test"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = local.load_test_addresses
}

resource "aws_wafv2_web_acl" "api" {
  #checkov:skip=CKV2_AWS_31:El logging completo del WAF a 10.000 RPS tiene un costo alto. Cada regla ya publica metricas y muestras de peticiones en CloudWatch; en produccion se activaria con filtro para registrar solo los bloqueos.
  name  = local.name
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # Excepción temporal para el generador de la prueba de carga.
  dynamic "rule" {
    for_each = aws_wafv2_ip_set.load_test

    content {
      name     = "allow-load-test"
      priority = 0

      action {
        allow {}
      }

      statement {
        ip_set_reference_statement {
          arn = rule.value.arn
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${local.name}-allow-load-test"
        sampled_requests_enabled   = true
      }
    }
  }

  # Límite de peticiones por IP.
  rule {
    name     = "rate-limit-per-ip"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Reglas administradas por AWS: ataques web comunes (OWASP Top 10).
  rule {
    name     = "aws-common-rules"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common-rules"
      sampled_requests_enabled   = true
    }
  }

  # Reglas administradas por AWS: entradas maliciosas conocidas.
  rule {
    name     = "aws-known-bad-inputs"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = aws_lb.api.arn
  web_acl_arn  = aws_wafv2_web_acl.api.arn
}
