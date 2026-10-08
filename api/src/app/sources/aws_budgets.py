"""Fuente real: lee los presupuestos de la cuenta desde AWS Budgets.

La usa solo el recolector. Las credenciales las entrega AWS al pod mediante su
rol de IAM (EKS Pod Identity); el único permiso es ver presupuestos. AWS
actualiza el gasto unas pocas veces al día, así que se consulta cada 15 minutos.
"""

import asyncio
from typing import Any

import boto3

from app.models import Budget, BudgetStatus, Money

# AWS Budgets es un servicio global que se atiende desde us-east-1.
BUDGETS_REGION = "us-east-1"
WARNING_THRESHOLD = 80.0


def budget_status(percent_used: float, forecast: float | None, limit: float) -> BudgetStatus:
    if percent_used >= 100:
        return "exceeded"
    if percent_used >= WARNING_THRESHOLD or (forecast is not None and forecast > limit):
        return "warning"
    return "ok"


def parse_budget(item: dict[str, Any]) -> Budget:
    limit = float(item["BudgetLimit"]["Amount"])
    unit = item["BudgetLimit"]["Unit"]
    spend = item.get("CalculatedSpend", {})
    actual = float(spend.get("ActualSpend", {}).get("Amount", 0))
    forecast_raw = spend.get("ForecastedSpend", {}).get("Amount")
    forecast = float(forecast_raw) if forecast_raw is not None else None
    percent = round(actual / limit * 100, 2) if limit else 0.0
    return Budget(
        name=item["BudgetName"],
        period=item.get("TimeUnit", "MONTHLY").lower(),
        limit=Money(amount=limit, unit=unit),
        actual_spend=Money(amount=actual, unit=unit),
        forecasted_spend=Money(amount=forecast, unit=unit) if forecast is not None else None,
        percent_used=percent,
        status=budget_status(percent, forecast, limit),
    )


class AwsBudgetSource:
    name = "aws-budgets"

    def __init__(self, session: Any | None = None) -> None:
        self._session = session or boto3.session.Session()
        self._account_id: str | None = None

    def _collect(self) -> list[Budget]:
        if self._account_id is None:
            self._account_id = self._session.client("sts").get_caller_identity()["Account"]
        client = self._session.client("budgets", region_name=BUDGETS_REGION)
        items = client.describe_budgets(AccountId=self._account_id).get("Budgets", [])
        return sorted((parse_budget(item) for item in items), key=lambda budget: budget.name)

    async def collect(self) -> list[Budget]:
        # boto3 es bloqueante: se ejecuta en un hilo para no frenar el servidor.
        return await asyncio.to_thread(self._collect)
