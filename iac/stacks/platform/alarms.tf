# Monitoreo básico fuera del clúster: alarmas de CloudWatch sobre el ALB y sobre
# la frescura de los datos que sirve la API.
# Si el clúster entero falla, Prometheus cae con él; estas alarmas no.
resource "aws_sns_topic" "alerts" {
  #checkov:skip=CKV_AWS_26:CloudWatch no puede publicar en un topic cifrado con la llave administrada por AWS (alias/aws/sns); exigiria una llave KMS propia. Los mensajes solo llevan el nombre y el estado de la alarma.
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
  api_dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

# Disponibilidad: la API está devolviendo errores 5xx.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-5xx"
  alarm_description   = "La API devuelve errores 5xx de forma sostenida"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = local.api_dimensions
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
  alarm_name          = "${local.name}-alb-5xx"
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

# Latencia: el 95 % de las peticiones debe responder en menos de 300 ms.
resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "${local.name}-latency-p95"
  alarm_description   = "La latencia p95 supera los 300 ms"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = local.api_dimensions
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

# Capacidad: hay pods que no pasan el health check del balanceador.
resource "aws_cloudwatch_metric_alarm" "unhealthy" {
  alarm_name          = "${local.name}-unhealthy-targets"
  alarm_description   = "Hay pods fuera de servicio en el balanceador"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = local.api_dimensions
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

# ---------- Frescura de los datos ----------
#
# La API responde desde una foto que mantiene el recolector. Si el recolector
# deja de sincronizar, la API sigue respondiendo 200 pero con datos viejos, y
# ninguna de las alarmas del ALB lo nota. El recolector publica 1 por cada
# sincronización buena y 0 por cada mala (métrica SyncSuccess); estas alarmas
# saltan si en la ventana no hubo ninguna buena.
#
# La falta de datos cuenta como falla (breaching): así también avisan si el
# recolector se muere, se queda sin permisos o no está desplegado.
locals {
  sync_alarms = {
    github = {
      description = "No se sincronizan los despliegues de GitHub hace 5 minutos (token vencido, límite agotado, repo inaccesible o recolector caído)"
      period      = 300
      evaluations = 1
    }
    cluster = {
      description = "No se sincroniza el estado del clúster hace 2 minutos (recolector caído o sin permisos de RBAC)"
      period      = 60
      evaluations = 2
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "sync" {
  for_each = local.sync_alarms

  alarm_name          = "${local.name}-sync-${each.key}"
  alarm_description   = each.value.description
  namespace           = var.project
  metric_name         = "SyncSuccess"
  dimensions          = { Environment = var.environment, Source = each.key }
  statistic           = "Maximum"
  period              = each.value.period
  evaluation_periods  = each.value.evaluations
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
