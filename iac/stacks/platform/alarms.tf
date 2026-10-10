# Monitoreo fuera del clúster: alarmas de CloudWatch sobre el ALB y sobre la
# frescura de los datos que sirve la API.
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
  alarm_actions  = [aws_sns_topic.alerts.arn]
  ticket_actions = [aws_sns_topic.tickets.arn]
  api_dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

# Disponibilidad y latencia se vigilan por SLO, con alarmas por consumo del
# presupuesto de error (slo.tf). Las de este archivo son de diagnóstico: apuntan
# a la causa probable y abren un ticket, pero no despiertan a nadie porque por sí
# solas no significan que el usuario esté afectado.

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
  alarm_actions       = local.ticket_actions
  ok_actions          = local.ticket_actions
}

# ---------- Frescura de los datos ----------
#
# La API responde desde una foto que mantiene el recolector. Si el recolector
# deja de sincronizar, la API sigue respondiendo 200 pero con datos viejos, y
# ninguna de las alarmas del ALB lo nota. El recolector publica 1 por cada
# sincronización buena y 0 por cada mala (métrica SyncSuccess); estas alarmas
# saltan si en la ventana no hubo ninguna buena. Abren ticket con el motivo; la
# que despierta es la de SLO de frescura (slo.tf), si el problema se sostiene.
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
  alarm_actions       = local.ticket_actions
  ok_actions          = local.ticket_actions
}
