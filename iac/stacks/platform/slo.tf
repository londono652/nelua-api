# SLOs y alertas por consumo del presupuesto de error (burn rate).
#
# Cada SLI se expresa como "porcentaje de eventos malos" en una ventana:
#   - disponibilidad: peticiones con 5xx (del ALB o de los pods) sobre el total
#   - latencia:       peticiones más lentas que el umbral sobre el total
#   - frescura:       sincronizaciones fallidas del recolector sobre el total
#
# El presupuesto de error es 100 - SLO (con 99,9 % se pueden fallar el 0,1 %).
# El burn rate es la velocidad a la que se gasta: 1 = se gasta justo en 30 días.
# Las alarmas no miran si "hay errores", sino si a este ritmo el presupuesto del
# mes se acaba antes de tiempo:
#
#   rápida: 14,4x en 1 h (2 % del presupuesto del mes en una hora)  -> despierta
#   lenta:   6x   en 6 h (5 % del presupuesto en seis horas)         -> despierta
#   ticket:  1x   tres días seguidos (se va a gastar todo en el mes) -> ticket
#
# Las dos primeras se confirman con una ventana corta (5 y 30 min) para que la
# alarma se apague apenas se corrige el problema y no siga sonando una hora.
locals {
  slo_budget = {
    availability = 100 - var.slo.availability
    latency      = 100 - var.slo.latency_target
    freshness    = 100 - var.slo.freshness
  }

  slo_description = {
    availability = "Disponibilidad: peticiones con 5xx (SLO ${var.slo.availability} %)"
    latency      = "Latencia: peticiones de más de ${var.slo.latency_threshold_seconds * 1000} ms (SLO ${var.slo.latency_target} %)"
    freshness    = "Frescura: sincronizaciones fallidas del recolector (SLO ${var.slo.freshness} %)"
  }

  # Métricas que componen cada SLI. El periodo lo pone cada alarma.
  sli_metrics = {
    availability = [
      { id = "t5", namespace = "AWS/ApplicationELB", metric = "HTTPCode_Target_5XX_Count", stat = "Sum", dims = local.api_dimensions },
      { id = "e5", namespace = "AWS/ApplicationELB", metric = "HTTPCode_ELB_5XX_Count", stat = "Sum", dims = { LoadBalancer = aws_lb.api.arn_suffix } },
      { id = "req", namespace = "AWS/ApplicationELB", metric = "RequestCount", stat = "Sum", dims = { LoadBalancer = aws_lb.api.arn_suffix } },
    ]
    # PR(:x) = porcentaje de peticiones que respondieron en x segundos o menos.
    latency = [
      { id = "fast", namespace = "AWS/ApplicationELB", metric = "TargetResponseTime", stat = "PR(:${var.slo.latency_threshold_seconds})", dims = local.api_dimensions },
    ]
    # El recolector publica 1 por sincronización buena y 0 por mala; el promedio
    # es la fracción de sincronizaciones buenas. Se toma la peor de las dos fuentes.
    freshness = [
      { id = "okc", namespace = var.project, metric = "SyncSuccess", stat = "Average", dims = { Environment = var.environment, Source = "cluster" } },
      { id = "okg", namespace = var.project, metric = "SyncSuccess", stat = "Average", dims = { Environment = var.environment, Source = "github" } },
    ]
  }

  # Porcentaje de eventos malos en la ventana.
  sli_bad_expression = {
    availability = "100 * (FILL(t5, 0) + FILL(e5, 0)) / req"
    latency      = "100 - fast"
    freshness    = "100 * (1 - MIN([FILL(okc, 0), FILL(okg, 0)]))"
  }

  # Sin tráfico no hay peticiones malas; sin sincronizaciones, los datos se están
  # poniendo viejos (recolector caído o sin desplegar).
  sli_missing_data = {
    availability = "notBreaching"
    latency      = "notBreaching"
    freshness    = "breaching"
  }

  burn_tiers = {
    fast = { burn = 14.4, long = 3600, short = 300, label = "1h", short_label = "5m" }
    slow = { burn = 6, long = 21600, short = 1800, label = "6h", short_label = "30m" }
  }

  # Alarmas de métrica: ventana larga y corta de cada nivel, más la de ticket.
  slo_alarms = merge(
    merge([
      for sli in keys(local.slo_budget) : {
        for pair in setproduct(keys(local.burn_tiers), ["long", "short"]) :
        "${sli}-${pair[0]}-${pair[1]}" => {
          sli         = sli
          period      = local.burn_tiers[pair[0]][pair[1]]
          evaluations = 1
          burn        = local.burn_tiers[pair[0]].burn
          window      = pair[1] == "long" ? local.burn_tiers[pair[0]].label : local.burn_tiers[pair[0]].short_label
          actions     = [] # avisa la alarma compuesta
        }
      }
    ]...),
    {
      for sli in keys(local.slo_budget) : "${sli}-ticket" => {
        sli         = sli
        period      = 86400
        evaluations = 3
        burn        = 1
        window      = "3 días"
        actions     = [aws_sns_topic.tickets.arn]
      }
    }
  )
}

resource "aws_sns_topic" "tickets" {
  #checkov:skip=CKV_AWS_26:Mismo motivo que el topic de alertas: CloudWatch no publica en un topic cifrado con la llave administrada por AWS.
  name = "${local.name}-tickets"
}

resource "aws_sns_topic_subscription" "tickets_email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.tickets.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "slo" {
  for_each = local.slo_alarms

  alarm_name          = "${local.name}-slo-${each.key}"
  alarm_description   = "${local.slo_description[each.value.sli]}. Burn rate ${each.value.burn}x en ${each.value.window}: más de ${format("%.2f", each.value.burn * local.slo_budget[each.value.sli])} % de eventos malos"
  comparison_operator = "GreaterThanThreshold"
  threshold           = tonumber(format("%.4f", each.value.burn * local.slo_budget[each.value.sli]))
  evaluation_periods  = each.value.evaluations
  datapoints_to_alarm = each.value.evaluations
  treat_missing_data  = local.sli_missing_data[each.value.sli]
  alarm_actions       = each.value.actions
  ok_actions          = each.value.actions

  metric_query {
    id          = "bad"
    expression  = local.sli_bad_expression[each.value.sli]
    label       = "% de eventos malos"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = local.sli_metrics[each.value.sli]
    content {
      id          = metric_query.value.id
      return_data = false
      metric {
        namespace   = metric_query.value.namespace
        metric_name = metric_query.value.metric
        dimensions  = metric_query.value.dims
        stat        = metric_query.value.stat
        period      = each.value.period
      }
    }
  }
}

# Despierta a alguien solo si la ventana larga confirma que el problema es real
# y la corta confirma que sigue pasando.
resource "aws_cloudwatch_composite_alarm" "slo_page" {
  for_each = {
    for pair in setproduct(keys(local.slo_budget), keys(local.burn_tiers)) :
    "${pair[0]}-${pair[1]}" => { sli = pair[0], tier = pair[1] }
  }

  alarm_name        = "${local.name}-slo-${each.key}"
  alarm_description = "${local.slo_description[each.value.sli]}. El presupuesto de error del mes se está gastando a ${local.burn_tiers[each.value.tier].burn}x"
  alarm_rule        = "ALARM(\"${aws_cloudwatch_metric_alarm.slo["${each.key}-long"].alarm_name}\") AND ALARM(\"${aws_cloudwatch_metric_alarm.slo["${each.key}-short"].alarm_name}\")"
  alarm_actions     = local.alarm_actions
  ok_actions        = local.alarm_actions
}

# ---------- Tablero ----------
#
# Por cada SLI: cumplimiento en 30 días, presupuesto restante y la serie de
# eventos malos por hora con las líneas de las alarmas.
locals {
  dashboard_metrics = {
    for sli, metrics in local.sli_metrics : sli => concat(
      [
        for m in metrics : concat(
          [m.namespace, m.metric],
          flatten([for k, v in m.dims : [k, v]]),
          [{ id = m.id, stat = m.stat, visible = false }]
        )
      ],
      [[{ id = "bad", expression = local.sli_bad_expression[sli], visible = false }]]
    )
  }

  slo_titles = {
    availability = "Disponibilidad"
    latency      = "Latencia (< ${var.slo.latency_threshold_seconds * 1000} ms)"
    freshness    = "Frescura"
  }

  slo_order = ["availability", "latency", "freshness"]

  dashboard_widgets = flatten([
    for i, sli in local.slo_order : [
      {
        type   = "metric"
        x      = i * 8
        y      = 0
        width  = 8
        height = 4
        properties = {
          title                = "${local.slo_titles[sli]}: SLI 30 días (SLO ${100 - local.slo_budget[sli]} %)"
          view                 = "singleValue"
          region               = var.region
          period               = 2592000
          setPeriodToTimeRange = true
          metrics              = concat(local.dashboard_metrics[sli], [[{ expression = "100 - bad", label = "% bueno", id = "sli" }]])
        }
      },
      {
        type   = "metric"
        x      = i * 8
        y      = 4
        width  = 8
        height = 4
        properties = {
          title                = "${local.slo_titles[sli]}: presupuesto de error restante"
          view                 = "singleValue"
          region               = var.region
          period               = 2592000
          setPeriodToTimeRange = true
          metrics              = concat(local.dashboard_metrics[sli], [[{ expression = "100 - 100 * bad / ${local.slo_budget[sli]}", label = "% del presupuesto", id = "budget" }]])
        }
      },
      {
        type   = "metric"
        x      = i * 8
        y      = 8
        width  = 8
        height = 6
        properties = {
          title   = "${local.slo_titles[sli]}: % de eventos malos por hora"
          view    = "timeSeries"
          region  = var.region
          period  = 3600
          metrics = concat(local.dashboard_metrics[sli], [[{ expression = "bad", label = "% malos", id = "badline" }]])
          annotations = {
            horizontal = [
              { label = "Ticket (1x)", value = local.slo_budget[sli] },
              { label = "Page lenta (6x)", value = 6 * local.slo_budget[sli] },
              { label = "Page rápida (14,4x)", value = 14.4 * local.slo_budget[sli] },
            ]
          }
          yAxis = { left = { min = 0 } }
        }
      },
    ]
  ])
}

resource "aws_cloudwatch_dashboard" "slo" {
  dashboard_name = "${local.name}-slo"
  dashboard_body = jsonencode({
    start   = "-P30D"
    widgets = local.dashboard_widgets
  })
}
