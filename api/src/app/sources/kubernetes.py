"""Fuente real: lee el estado del clúster desde la API de Kubernetes.

La usa el recolector, con su cuenta de servicio (token y CA montados por
Kubernetes) y permisos de solo lectura sobre deployments, replicasets, pods y
autoescaladores.
Hace unas pocas llamadas GET por ciclo; no depende de librerías pesadas.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.models import (
    Autoscaling,
    ClusterState,
    Deployment,
    DeploymentStatus,
    Pod,
    Replicas,
    Revision,
    RevisionReplicas,
)

REVISION = "deployment.kubernetes.io/revision"
REVISION_HISTORY = "deployment.kubernetes.io/revision-history"


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _condition(item: dict[str, Any], kind: str) -> dict[str, Any]:
    for condition in item.get("status", {}).get("conditions", []):
        if condition.get("type") == kind:
            return condition
    return {}


def deployment_status(desired: int, ready: int, updated: int, item: dict[str, Any]) -> str:
    """Traduce las condiciones de Kubernetes a un estado simple para quien consume."""
    if desired == 0:
        return "scaled_down"
    if ready >= desired and updated >= desired:
        return "healthy"
    if _condition(item, "Available").get("status") == "False":
        return "unavailable"
    progressing = _condition(item, "Progressing")
    if (
        progressing.get("status") == "True"
        and progressing.get("reason") != "NewReplicaSetAvailable"
    ):
        return "progressing"
    return "degraded"


def split_image(image: str) -> str:
    """Devuelve la versión (tag o digest) de una imagen de contenedor."""
    if "@" in image:
        return image.split("@", 1)[1]
    name = image.rsplit("/", 1)[-1]
    return name.split(":", 1)[1] if ":" in name else "latest"


def parse_pod(item: dict[str, Any]) -> Pod:
    statuses = item.get("status", {}).get("containerStatuses", [])
    return Pod(
        name=item["metadata"]["name"],
        phase=item.get("status", {}).get("phase", "Unknown"),
        ready=bool(statuses) and all(status.get("ready") for status in statuses),
        restarts=sum(status.get("restartCount", 0) for status in statuses),
        node=item.get("spec", {}).get("nodeName"),
        started_at=_parse_time(item.get("status", {}).get("startTime")),
    )


def parse_autoscaling(item: dict[str, Any]) -> Autoscaling:
    spec, status = item.get("spec", {}), item.get("status", {})
    cpu_target = cpu_current = None
    for metric in spec.get("metrics", []):
        resource = metric.get("resource", {})
        if resource.get("name") == "cpu":
            cpu_target = resource.get("target", {}).get("averageUtilization")
    for metric in status.get("currentMetrics") or []:
        resource = metric.get("resource", {})
        if resource.get("name") == "cpu":
            cpu_current = resource.get("current", {}).get("averageUtilization")
    return Autoscaling(
        min_replicas=spec.get("minReplicas", 1),
        max_replicas=spec.get("maxReplicas", 1),
        current_replicas=status.get("currentReplicas", 0),
        desired_replicas=status.get("desiredReplicas", 0),
        cpu_target_percent=cpu_target,
        cpu_current_percent=cpu_current,
    )


def parse_deployment(
    item: dict[str, Any], pods: list[dict[str, Any]], autoscalers: list[dict[str, Any]]
) -> Deployment:
    metadata, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
    desired = spec.get("replicas", 0)
    ready = status.get("readyReplicas", 0)
    updated = status.get("updatedReplicas", 0)
    image = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "")

    # Pods del deployment: los que cumplen su selector de etiquetas.
    selector = spec.get("selector", {}).get("matchLabels", {})
    own_pods = [
        parse_pod(pod)
        for pod in pods
        if selector
        and all(pod["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
    ]

    autoscaling = None
    for autoscaler in autoscalers:
        target = autoscaler.get("spec", {}).get("scaleTargetRef", {})
        if target.get("kind") == "Deployment" and target.get("name") == metadata["name"]:
            autoscaling = parse_autoscaling(autoscaler)

    state: DeploymentStatus = deployment_status(desired, ready, updated, item)  # type: ignore[assignment]
    return Deployment(
        namespace=metadata["namespace"],
        name=metadata["name"],
        status=state,
        replicas=Replicas(
            desired=desired,
            ready=ready,
            updated=updated,
            available=status.get("availableReplicas", 0),
        ),
        image=image,
        version=split_image(image),
        updated_at=_parse_time(_condition(item, "Progressing").get("lastUpdateTime")),
        autoscaling=autoscaling,
        pods=sorted(own_pods, key=lambda pod: pod.name),
    )


def parse_revisions(
    replicasets: list[dict[str, Any]], deployments: list[dict[str, Any]]
) -> list[Revision]:
    """Arma el historial de despliegues a partir de los ReplicaSets.

    Kubernetes crea un ReplicaSet por cada versión desplegada y le pone un
    número de revisión. En un rollback no crea uno nuevo: reactiva el anterior
    con un número de revisión mayor y anota los números que tuvo antes.
    """
    current = {
        item["metadata"]["name"]: item["metadata"].get("annotations", {}).get(REVISION)
        for item in deployments
    }
    revisions = []
    for item in replicasets:
        metadata = item["metadata"]
        annotations = metadata.get("annotations", {})
        owner = next(
            (
                ref["name"]
                for ref in metadata.get("ownerReferences", [])
                if ref.get("kind") == "Deployment"
            ),
            None,
        )
        if owner is None or not annotations.get(REVISION, "").isdigit():
            continue
        spec = item.get("spec", {})
        image = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "")
        revisions.append(
            Revision(
                namespace=metadata["namespace"],
                deployment=owner,
                revision=int(annotations[REVISION]),
                version=split_image(image),
                image=image,
                created_at=_parse_time(metadata.get("creationTimestamp")),
                active=annotations[REVISION] == current.get(owner),
                reactivated=bool(annotations.get(REVISION_HISTORY)),
                replicas=RevisionReplicas(
                    desired=spec.get("replicas", 0),
                    ready=item.get("status", {}).get("readyReplicas", 0),
                ),
            )
        )
    return revisions


class KubernetesSource:
    name = "kubernetes"

    def __init__(
        self,
        api_url: str,
        token_file: str,
        ca_file: str,
        namespaces: tuple[str, ...],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_file = Path(token_file)
        self._namespaces = namespaces
        verify: bool | str = ca_file if transport is None else True
        self._client = httpx.AsyncClient(
            base_url=api_url, verify=verify, timeout=10, transport=transport
        )

    async def _list(self, path: str) -> list[dict[str, Any]]:
        # El token de la cuenta de servicio rota: se lee del disco en cada llamada.
        token = self._token_file.read_text(encoding="utf-8").strip()
        response = await self._client.get(path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json().get("items", [])

    async def collect(self) -> ClusterState:
        deployments: list[Deployment] = []
        revisions: list[Revision] = []
        for namespace in self._namespaces:
            items = await self._list(f"/apis/apps/v1/namespaces/{namespace}/deployments")
            pods = await self._list(f"/api/v1/namespaces/{namespace}/pods")
            autoscalers = await self._list(
                f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers"
            )
            replicasets = await self._list(f"/apis/apps/v1/namespaces/{namespace}/replicasets")
            deployments += [parse_deployment(item, pods, autoscalers) for item in items]
            revisions += parse_revisions(replicasets, items)

        return ClusterState(
            deployments=sorted(deployments, key=lambda d: (d.namespace, d.name)),
            revisions=revisions,
        )

    async def close(self) -> None:
        await self._client.aclose()
