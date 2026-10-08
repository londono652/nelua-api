"""Contratos de las fuentes de datos.

El recolector no sabe de dónde vienen los datos: solo conoce estas interfaces.
En el clúster se usan las implementaciones reales (Kubernetes y GitHub); en
desarrollo local y en las pruebas, las de ejemplo.
"""

from typing import Protocol

from app.models import ClusterState, Deploy


class ClusterSource(Protocol):
    name: str

    async def collect(self) -> ClusterState: ...


class DeploySource(Protocol):
    name: str

    async def list_deploys(self, repo: str, known: dict[int, Deploy]) -> list[Deploy]:
        """Devuelve los despliegues recientes de un repositorio ("owner/repo").
        Los que ya están en "known" y terminaron no se vuelven a consultar."""
        ...
