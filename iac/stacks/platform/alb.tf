# El ALB lo crea Terraform (no Kubernetes): así el pipeline de infraestructura
# es independiente de la aplicación. Los pods se registran en los target groups
# mediante un TargetGroupBinding.

data "aws_ssm_parameter" "certificate_arn" {
  name = "/${var.project}/acm/certificate-arn"
}

data "aws_route53_zone" "main" {
  name         = var.domain
  private_zone = false
}

locals {
  # Dos entornos en el mismo clúster, cada uno con su nombre público.
  environments = {
    prod    = "api.${var.domain}"
    staging = "api-staging.${var.domain}"
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "ALB publico de ${var.project}: solo HTTPS"
  vpc_id      = module.vpc.vpc_id

  tags = {
    Name = "${var.project}-alb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS desde internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_to_pods" {
  security_group_id = aws_security_group.alb.id
  description       = "Hacia los pods de la API"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = var.app_port
  to_port           = var.app_port
}

# Los pods solo aceptan tráfico de la API desde el ALB.
resource "aws_vpc_security_group_ingress_rule" "pods_from_alb" {
  security_group_id            = module.eks.cluster_primary_security_group_id
  description                  = "API desde el ALB"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.app_port
  to_port                      = var.app_port
}

resource "aws_lb" "api" {
  #checkov:skip=CKV2_AWS_76:Falso positivo. El WAF asociado si incluye AWSManagedRulesKnownBadInputsRuleSet (la que cubre Log4j); checkov no lo detecta por el bloque dynamic de la web ACL.
  #checkov:skip=CKV_AWS_150:La plataforma es efimera (se destruye con ops-down para no generar costo). En un entorno permanente se activaria la proteccion contra borrado.
  #checkov:skip=CKV_AWS_91:Los access logs a 10.000 RPS generan un volumen y costo altos en S3. La observabilidad se cubre con metricas de Prometheus y CloudWatch; en produccion se activarian con muestreo o retencion corta.
  name               = var.project
  load_balancer_type = "application"
  internal           = false
  subnets            = module.vpc.public_subnets
  security_groups    = [aws_security_group.alb.id]

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  #checkov:skip=CKV_AWS_378:TLS termina en el ALB. El tramo ALB -> pods viaja dentro de la VPC, en subnets privadas, y un security group solo lo permite desde el ALB. Cifrarlo tambien exigiria certificados en los pods o un service mesh.
  for_each = local.environments

  name        = "${var.project}-${each.key}"
  vpc_id      = module.vpc.vpc_id
  port        = var.app_port
  protocol    = "HTTP"
  target_type = "ip"

  # Tiempo que el ALB espera antes de sacar un pod que se está apagando.
  deregistration_delay = 30

  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  # EKS Auto Mode solo puede registrar pods en target groups de SU clúster.
  tags = {
    "eks:eks-cluster-name" = var.project
  }
}

# Único listener: HTTPS. El puerto 80 no se abre (es una API, no un sitio web).
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = data.aws_ssm_parameter.certificate_arn.value

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = "{\"detail\":\"Not found\"}"
      status_code  = "404"
    }
  }
}

# Enruta por nombre: api.nelua.site -> prod, api-staging.nelua.site -> staging.
resource "aws_lb_listener_rule" "api" {
  for_each = local.environments

  listener_arn = aws_lb_listener.https.arn

  condition {
    host_header {
      values = [each.value]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[each.key].arn
  }
}

resource "aws_route53_record" "api" {
  for_each = local.environments

  zone_id = data.aws_route53_zone.main.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.api.dns_name
    zone_id                = aws_lb.api.zone_id
    evaluate_target_health = true
  }
}
