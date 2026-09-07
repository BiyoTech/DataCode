"""Sandbox inventory — what the cluster is actually running for this cell.

The reconciler's sandbox drift kinds (``orphan_codeloc_pod``, ``zombie_task``)
need one fact the control plane cannot obtain itself: which user-code Pods
exist. Only workspace-runtime holds a cluster client (control-service is
deliberately light and has no Kubernetes SDK by default), so the runtime
answers, and the control plane compares that answer against its registry.

This module is only the OBSERVER plus the narrowest possible reclaim. It makes
no judgement about whether a Pod should exist — that comparison needs the
registry, which lives on the other side. Keeping the decision there and the
mechanism here is the same split every other binder follows.

Why deletion is name-based and vehicle-scoped
---------------------------------------------
``delete_code_location_pod`` deletes by exact name within the user-code
namespace, and only objects carrying the code-location app label. A reconciler
acting on a stale read must not be able to remove a Pod that has since been
recreated for a live environment: the label check plus the environment argument
make an accidental match require two independent coincidences.

Everything degrades to "unknown" rather than "empty": a cluster read failure
returns ``None``, and the reconciler withholds every sandbox verdict for that
pass — exactly the rule the deployment and SQLMesh binders already follow.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CodeLocationPodInfo:
    """One code-location Pod as the cluster reports it."""

    pod_name: str
    environment: str
    location_name: str
    phase: str

    def as_dict(self) -> dict:
        return {
            "pod_name": self.pod_name,
            "environment": self.environment,
            "location_name": self.location_name,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class TaskPodInfo:
    """One task sandbox Pod as the cluster reports it."""

    pod_name: str
    task_id: str
    tenant_id: str
    phase: str

    def as_dict(self) -> dict:
        return {
            "pod_name": self.pod_name,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "phase": self.phase,
        }


class SandboxInventory:
    """Lists and (narrowly) removes the cell's user-code Pods."""

    def __init__(self, *, core_api: t.Optional[t.Any] = None) -> None:
        # Injected in tests. Resolved lazily in production because the cluster
        # client is an optional dependency: a dev install has no kubernetes SDK,
        # and importing it at module scope would break every process vehicle.
        self._core = core_api

    @property
    def available(self) -> bool:
        """Whether this runtime can see the cluster at all.

        False in every non-Kubernetes deployment (dev, compose). The reconciler
        distinguishes this from a read FAILURE: a cell with no container plane
        has no sandbox party to reconcile, which is not drift.
        """
        return bool(self._resolve_core())

    async def list_code_location_pods(self) -> t.Optional[list[CodeLocationPodInfo]]:
        """Code-location Pods in the user-code namespace, or ``None`` if unreadable."""
        core = self._resolve_core()
        if core is None:
            return None

        from src.codeloc.pod import (
            APP_CODE_LOCATION,
            LABEL_APP,
            LABEL_ENVIRONMENT,
            LABEL_LOCATION,
            code_location_namespace,
        )

        pods = await self._list_pods(
            core, code_location_namespace(), f"{LABEL_APP}={APP_CODE_LOCATION}"
        )
        if pods is None:
            return None
        return [
            CodeLocationPodInfo(
                pod_name=self._name_of(pod),
                environment=self._label_of(pod, LABEL_ENVIRONMENT),
                location_name=self._label_of(pod, LABEL_LOCATION),
                phase=self._phase_of(pod),
            )
            for pod in pods
        ]

    async def list_task_pods(self) -> t.Optional[list[TaskPodInfo]]:
        """Task sandbox Pods in the task namespace, or ``None`` if unreadable."""
        core = self._resolve_core()
        if core is None:
            return None

        from src.tasks.backends.kubernetes import (
            APP_TASK_RUNNER,
            LABEL_APP,
            LABEL_TASK,
            LABEL_TENANT,
        )

        pods = await self._list_pods(
            core, settings.task_namespace, f"{LABEL_APP}={APP_TASK_RUNNER}"
        )
        if pods is None:
            return None
        return [
            TaskPodInfo(
                pod_name=self._name_of(pod),
                task_id=self._label_of(pod, LABEL_TASK),
                tenant_id=self._label_of(pod, LABEL_TENANT),
                phase=self._phase_of(pod),
            )
            for pod in pods
        ]

    async def delete_code_location_pod(
        self, *, pod_name: str, environment: str
    ) -> bool:
        """Delete one code-location Pod and its Service. Never raises.

        Both arguments must agree with the object's labels: the pass that
        decided to delete may be a minute old, and a Pod recreated meanwhile for
        a live environment must survive. Returns whether the Pod was removed;
        the Service is best-effort, and a leftover Service resolves to nothing
        rather than to the wrong Pod.
        """
        core = self._resolve_core()
        if core is None or not pod_name:
            return False

        from src.codeloc.pod import (
            APP_CODE_LOCATION,
            LABEL_APP,
            LABEL_ENVIRONMENT,
            code_location_namespace,
            service_name,
        )

        namespace = code_location_namespace()
        try:
            pod = await asyncio.to_thread(
                core.read_namespaced_pod, pod_name, namespace
            )
        except Exception as exc:  # noqa: BLE001 — already gone is a success
            logger.info(
                "orphan_codeloc_pod_absent",
                pod=pod_name,
                environment=environment,
                detail=str(exc),
            )
            return False

        labels = self._labels_of(pod)
        if labels.get(LABEL_APP) != APP_CODE_LOCATION:
            logger.warning(
                "orphan_codeloc_pod_refused",
                pod=pod_name,
                reason="not a code-location Pod",
            )
            return False
        if environment and labels.get(LABEL_ENVIRONMENT) != environment:
            # The name was reused for a different environment between the read
            # and this write. Refusing is the whole point of re-checking.
            logger.warning(
                "orphan_codeloc_pod_refused",
                pod=pod_name,
                expected_environment=environment,
                actual_environment=labels.get(LABEL_ENVIRONMENT, ""),
                reason="environment label changed since it was observed",
            )
            return False

        deleted = await self._delete(core.delete_namespaced_pod, pod_name, namespace)
        if environment:
            await self._delete(
                core.delete_namespaced_service, service_name(environment), namespace
            )
        if deleted:
            logger.info(
                "orphan_codeloc_pod_deleted",
                pod=pod_name,
                environment=environment,
                namespace=namespace,
            )
        return deleted

    # ── internals ──────────────────────────────────────────────────────

    def _resolve_core(self) -> t.Optional[t.Any]:
        """The CoreV1 client, or ``None`` when this deployment has no cluster."""
        if self._core is not None:
            return self._core
        if (settings.task_backend or "").strip().lower() != "kubernetes":
            # No container plane configured: there are no sandbox Pods to see,
            # and constructing a cluster client would fail for the wrong reason.
            return None
        try:
            from src.tasks.backends.kubernetes import KubernetesBackend

            self._core = KubernetesBackend()._core
        except Exception as exc:  # noqa: BLE001 — optional dependency / no config
            logger.warning("sandbox_inventory_client_unavailable", error=str(exc))
            return None
        return self._core

    @staticmethod
    async def _list_pods(
        core, namespace: str, label_selector: str
    ) -> t.Optional[list[t.Any]]:
        try:
            listing = await asyncio.to_thread(
                core.list_namespaced_pod, namespace, label_selector=label_selector
            )
        except Exception as exc:  # noqa: BLE001 — unreadable, not empty
            logger.warning(
                "sandbox_inventory_list_failed",
                namespace=namespace,
                selector=label_selector,
                error=str(exc),
            )
            return None
        return list(getattr(listing, "items", None) or [])

    @staticmethod
    async def _delete(operation, name: str, namespace: str) -> bool:
        try:
            await asyncio.to_thread(operation, name, namespace)
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            logger.warning(
                "sandbox_inventory_delete_failed",
                name=name,
                namespace=namespace,
                error=str(exc),
            )
            return False
        return True

    @staticmethod
    def _labels_of(pod: t.Any) -> dict[str, str]:
        metadata = getattr(pod, "metadata", None)
        return dict(getattr(metadata, "labels", None) or {})

    @classmethod
    def _label_of(cls, pod: t.Any, key: str) -> str:
        return cls._labels_of(pod).get(key, "")

    @staticmethod
    def _name_of(pod: t.Any) -> str:
        metadata = getattr(pod, "metadata", None)
        return str(getattr(metadata, "name", "") or "")

    @staticmethod
    def _phase_of(pod: t.Any) -> str:
        """Pod phase, lowercased. Empty when the status is not populated yet."""
        status = getattr(pod, "status", None)
        return str(getattr(status, "phase", "") or "").lower()


_inventory: t.Optional[SandboxInventory] = None


def get_sandbox_inventory() -> SandboxInventory:
    """Get or create the shared :class:`SandboxInventory`."""
    global _inventory
    if _inventory is None:
        _inventory = SandboxInventory()
    return _inventory


__all__ = [
    "CodeLocationPodInfo",
    "SandboxInventory",
    "TaskPodInfo",
    "get_sandbox_inventory",
]
