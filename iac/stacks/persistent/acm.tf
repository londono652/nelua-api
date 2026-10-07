# La zona DNS ya existe (stack bootstrap); aquí solo se lee.
data "aws_route53_zone" "main" {
  name         = var.domain
  private_zone = false
}

# Certificado público gratuito, emitido solo para los dos nombres que se usan
# (sin comodín): api.nelua.site (prod) y api-staging.nelua.site (staging).
resource "aws_acm_certificate" "api" {
  domain_name               = "api.${var.domain}"
  subject_alternative_names = ["api-staging.${var.domain}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Registros que le demuestran a ACM que los dos nombres son nuestros.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for option in aws_acm_certificate.api.domain_validation_options : option.domain_name => {
      name   = option.resource_record_name
      type   = option.resource_record_type
      record = option.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# Espera a que ACM valide y emita el certificado.
resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}
