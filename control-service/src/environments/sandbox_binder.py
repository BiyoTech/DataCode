"""Sandbox binder — the reconciler's sandbox party (design 05 §5.1).

``orphan_codeloc_pod`` and ``zombie_task`` compare the registry against what the
cluster is actually running. The control plane owns the registry but holds no
cluster client, so this binder asks workspace-runtime (which does) over the
env-control channel — the same shape ``GitBinder`` and ``SqlMeshBinder`` use, and
for the same reason: the control plane stays free of infrastructure clients.

The honesty contract, which every caller depends on
---------------------------------------------------
Three distinguishable outcomes, never collapsed into two:

* ``None``          — the cluster could not be read. NO verdict follows; the
                      reconciler reports nothing about sandboxes this pass.
* ``[]``            — read successfully, nothing running. Verdicts follow.
* ``unavailable``   — this deployment has no container plane at all (dev,
                      compose). Also no verdict, but permanently and benignly so,
                      which is why it is separate from a failed read.

Collapsing the first into the second is the mistake that would make a routine
runtime restart delete every code-location Pod in the cell.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from src.runtime.client import get_runtime_client
from src.utils.errors import ServiceError
from src.utils.logging import get_logger

logger = get_logger(__name__)

_INVENTORY_PATH = "/internal/env-control/sandbox/inventory"
_DELETE_CODELOC_POD_PATH = "/internal/env-control/sandbox/delete-code-location-pod"


@dataclass(frozen=True)
class SandboxWorld:
    """What the cluster is running, as one pass observed it.

    ``code_location_pods`` / ``task_pods`` are ``None`` when that half could not
    be read; ``available`` is False when there is no container plane to read.
    """

    available: bool
    code_location_pods: t.Optional[t.List[t.Dict[str, t.Any]]]
    task_pods: t.Optional[t.List[t.Dict[str, t.Any]]]

    @property
    def running_task_ids(self) -> t.Optional[frozenset[str]]:
        """Task ids with a Pod, or ``None`` when the task half was unreadable.

        Every phase counts as "running", including ``pending``: a Pod waiting to
        be scheduled is exactly the state a task legitimately occupies for its
        first seconds, and treating it as absent would mark every young task a
        zombie.
        """
        if self.task_pods is None:
            return None
        return frozenset(
            str(pod.get("task_id", "")) for pod in self.task_pods if pod.get("task_id")
        )

    def code_location_pods_by_environment(
        self,
    ) -> t.Optional[t.Dict[str, t.List[t.Dict[str, t.Any]]]]:
        """Code-location Pods grouped by the environment they serve."""
        if self.code_location_pods is None:
            return None
        grouped: t.Dict[str, t.List[t.Dict[str, t.Any]]] = {}
        for pod in self.code_location_pods:
            environment = str(pod.get("environment", ""))
            if not environment:
                # A code-location Pod without its environment label cannot be
                # attributed to a registry row, so it can neither be confirmed
                # nor condemned. Reported once, never deleted.
                logger.warning(
                    "codeloc_pod_unattributable", pod=pod.get("pod_name", "")
                )
                continue
            grouped.setdefault(environment, []).append(pod)
        return grouped


class SandboxBinder:
    """Reads the cluster's user-code Pods and reclaims orphaned ones."""

    def __init__(self, *, client: t.Optional[t.Any] = None) -> None:
        # Injected in tests; production resolves the shared runtime client.
        self._client = client

    async def read_world(self) -> t.Optional[SandboxWorld]:
        """Snapshot the sandbox party, or ``None`` when the runtime is unreachable.

        An unreachable RUNTIME and an unreadable CLUSTER produce the same
        verdict (no judgement), so they are folded together here — the
        distinction only matters for the log line, which records both.
        """
        client = self._client or get_runtime_client()
        try:
            payload = await client.get(_INVENTORY_PATH)
        except ServiceError as exc:
            logger.warning("sandbox_world_read_failed", error=str(exc))
            return None

        if not payload.get("available", False):
            logger.debug("sandbox_world_unavailable", reason="no container plane")
            return SandboxWorld(
                available=False, code_location_pods=None, task_pods=None
            )

        world = SandboxWorld(
            available=True,
            code_location_pods=_as_pod_list(payload.get("code_location_pods")),
            task_pods=_as_pod_list(payload.get("task_pods")),
        )
        logger.info(
            "sandbox_world_read",
            code_location_pods=(
                len(world.code_location_pods)
                if world.code_location_pods is not None
                else None
            ),
            task_pods=len(world.task_pods) if world.task_pods is not None else None,
        )
        return world

    async def delete_code_location_pod(
        self, *, pod_name: str, environment: str
    ) -> bool:
        """Ask the runtime to remove one orphaned code-location Pod.

        Returns False (never raises) when the runtime is unreachable or refused:
        an unreclaimed Pod is drift the next pass detects again, not a reason to
        abort the reconciliation that found it.
        """
        client = self._client or get_runtime_client()
        try:
            payload = await client.post(
                _DELETE_CODELOC_POD_PATH,
                {"pod_name": pod_name, "environment": environment},
            )
        except ServiceError as exc:
            logger.warning(
                "orphan_codeloc_pod_delete_failed",
                pod=pod_name,
                environment=environment,
                error=str(exc),
            )
            return False
        return bool(payload.get("deleted", False))


def _as_pod_list(value: t.Any) -> t.Optional[t.List[t.Dict[str, t.Any]]]:
    """Coerce the wire value into a pod list, preserving ``None``.

    A malformed body is treated as unreadable rather than empty: a 200 whose
    shape we do not recognize says nothing about what is running, and reading it
    as "nothing" is what would authorize deletions.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        logger.warning("sandbox_inventory_malformed", payload_type=type(value).__name__)
        return None
    return [item for item in value if isinstance(item, dict)]


_binder: t.Optional[SandboxBinder] = None


def get_sandbox_binder() -> SandboxBinder:
    """Get or create the global :class:`SandboxBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = SandboxBinder()
    return _binder


__all__ = ["SandboxBinder", "SandboxWorld", "get_sandbox_binder"]
