"""Contratos de las fuentes de datos.

La API no sabe de dónde vienen los datos: solo conoce estas dos interfaces.
En el clúster se usan las implementaciones reales (Kubernetes y AWS Budgets);
en desarrollo local y en las pruebas, la de ejemplo.
"""

from typing import Protocol

from app.models import Budget, ClusterState


class ClusterSource(Protocol):
    name: str

    async def collect(self) -> ClusterState: ...


class BudgetSource(Protocol):
    name: str

    async def collect(self) -> list[Budget]: ...
