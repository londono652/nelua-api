# Monitoreo básico fuera del clúster: alarmas de CloudWatch sobre el ALB.
# Si el clúster entero falla, Prometheus cae con él; estas alarmas no.
resource "aws_sns_topic" "alerts" {
  #checkov:skip=CKV_AWS_26:CloudWatch no puede publicar en un topic cifrado con la llave administrada por AWS (alias/aws/sns); exigiria una llave KMS propia. Los mensajes solo llevan el nombre y el estado de la alarma.
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
  prod_dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api["prod"].arn_suffix
  }
}

# Disponibilidad: la API de prod está devolviendo errores 5xx.
resource "aws_cloudwatch_metric_alarm" "prod_5xx" {
  alarm_name          = "${var.project}-prod-5xx"
  alarm_description   = "La API de prod devuelve errores 5xx de forma sostenida"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = local.prod_dimensions
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# El balanceador no logra entregar peticiones (no hay pods sanos o no responden).
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project}-alb-5xx"
  alarm_description   = "El ALB responde 5xx: no puede entregar peticiones a los pods"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Latencia: el 95 % de las peticiones de prod debe responder en menos de 300 ms.
resource "aws_cloudwatch_metric_alarm" "prod_latency" {
  alarm_name          = "${var.project}-prod-latency-p95"
  alarm_description   = "La latencia p95 de prod supera los 300 ms"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = local.prod_dimensions
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 0.3
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Capacidad: hay pods de prod que no pasan el health check del balanceador.
resource "aws_cloudwatch_metric_alarm" "prod_unhealthy" {
  alarm_name          = "${var.project}-prod-unhealthy-targets"
  alarm_description   = "Hay pods de prod fuera de servicio en el balanceador"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = local.prod_dimensions
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
