# Presupuesto mensual de la cuenta. Es de toda la cuenta y no de un ambiente,
# así que vive en la base compartida.
#
# Cumple dos papeles:
#   - el recolector lo lee cada 15 minutos y la API lo expone en GET /v1/budget;
#   - si hay correo configurado, AWS avisa al 80 % del gasto real y cuando el
#     pronóstico del mes supera el límite.
#
# AWS Budgets no cobra por los dos primeros presupuestos de la cuenta.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-mensual"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = var.budget_alert_email == "" ? [] : [
      { type = "ACTUAL", threshold = 80 },
      { type = "FORECASTED", threshold = 100 },
    ]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
