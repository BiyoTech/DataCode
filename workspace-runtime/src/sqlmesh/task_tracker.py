"""Task tracker for managing async background operations.

Tracks running plan/apply tasks per workspace to:
- Prevent conflicting concurrent operations
- Report running status via /meta endpoint
- Enable cancellation via circuit breaker

The local dict holds the LIVE task handle — only the replica that spawned
the task can act on it. The companion ``DistributedRunRegistry`` makes the
task's existence visible to every replica and routes cancellation back to the
owner, so:

- a second apply for a workspace is refused (409) wherever the request lands,
  not only on the replica already running it;
- cancel reaches the owning replica through a Redis broadcast even when the
  request is load-balanced elsewhere.

Degradation: without Redis both properties narrow to single-replica scope,
which is the pre-HA behaviour. Nothing raises.

Methods are async where they consult the registry; callers are request
handlers that already run in a loop.
"""

import asyncio
import time
from typing import Dict, Optional

from src.utils.logging import get_logger

logger = get_logger(__name__)


class TaskInfo:
    """Metadata for a running background task."""

    __slots__ = ("workspace_path", "task_type", "started_at", "task", "circuit_breaker")

    def __init__(
        self,
        workspace_path: str,
        task_type: str,
        task: asyncio.Task,
        circuit_breaker: asyncio.Event,
    ):
        self.workspace_path = workspace_path
        self.task_type = task_type
        self.started_at = time.time()
        self.task = task
        self.circuit_breaker = circuit_breaker


# How long a task's Redis registration survives. Applies run for minutes to
# hours; the TTL only bounds how long a crashed replica's stale registration
# blocks the workspace before self-reclaiming.
_TASK_TTL_SECONDS = 12 * 3600


class TaskTracker:
    """Tracks running async tasks (plan/apply) across workspaces and replicas."""

    def __init__(self):
        # Key: "{workspace_path}:{gateway}" → TaskInfo
        self._tasks: Dict[str, TaskInfo] = {}
        self._registry = None

    # -- registry wiring ---------------------------------------------------

    def _get_registry(self):
        """Lazily build the distributed registry (import avoids cycles)."""
        if self._registry is None:
            from datacode_common.redis import DistributedRunRegistry

            self._registry = DistributedRunRegistry(
                domain="sm", cancel_local=self._cancel_local
            )
        return self._registry

    async def start_registry(self) -> None:
        """Start listening for remote cancel requests (service startup)."""
        try:
            await self._get_registry().start()
        except Exception as exc:
            logger.warning("task_registry_start_failed", error=str(exc))

    async def stop_registry(self) -> None:
        """Release the registry's subscription (service shutdown)."""
        if self._registry is not None:
            await self._registry.stop()

    # -- queries -----------------------------------------------------------

    async def has_running_task(self, workspace_path: str = "") -> bool:
        """Check if any task is running (locally, or on any replica).

        Args:
            workspace_path: If provided, check only for that workspace.
                           If empty, check globally.
        """
        # Local check first: it is free and authoritative for this replica's
        # tasks. The remote check answers the case the dict cannot see — a
        # task running on ANOTHER replica.
        if self._has_running_local(workspace_path):
            return True

        if not workspace_path:
            # No cheap per-workspace remote probe for the global question:
            # check every locally-known key's remote twin. The global form is
            # only used by /meta for a status hint, so best-effort is fine.
            return any(
                await self._registry_exists(key)
                for key in self._tasks
            )
        # Remote keys are per "{workspace}:{gateway}"; a remote apply under
        # any gateway is still a running task for the workspace.
        return await self._any_remote_for_workspace(workspace_path)

    def _has_running_local(self, workspace_path: str = "") -> bool:
        if not workspace_path:
            return any(not t.task.done() for t in self._tasks.values())
        return any(
            not t.task.done()
            for key, t in self._tasks.items()
            if t.workspace_path == workspace_path
        )

    async def _any_remote_for_workspace(self, workspace_path: str) -> bool:
        try:
            from datacode_common.redis import get_redis, redis_key

            client = get_redis()
            if client is None:
                return False
            # SCAN over this workspace's remote keys regardless of gateway.
            pattern = redis_key("sm", "run", f"{workspace_path}:*")
            async for _key in client.scan_iter(match=pattern, count=100):
                return True
            return False
        except Exception:
            return False

    async def _registry_exists(self, key: str) -> bool:
        try:
            return await self._get_registry().exists(key)
        except Exception:
            return False

    # -- lifecycle ---------------------------------------------------------

    async def register(
        self,
        key: str,
        workspace_path: str,
        task_type: str,
        task: asyncio.Task,
        circuit_breaker: asyncio.Event,
    ) -> bool:
        """Register a new background task.

        Returns False when a task for the same key is already running on any
        replica — callers translate that into the same 409 as the local check.
        """
        # Clean up completed tasks
        self._cleanup_done()

        ok = True
        try:
            ok = await self._get_registry().register(
                key, ttl_seconds=_TASK_TTL_SECONDS
            )
        except Exception:
            pass  # degraded: local-only protection
        if not ok:
            logger.info(
                "task_already_running_remote", key=key, workspace_path=workspace_path
            )
            return False

        self._tasks[key] = TaskInfo(
            workspace_path=workspace_path,
            task_type=task_type,
            task=task,
            circuit_breaker=circuit_breaker,
        )
        logger.info(
            "task_registered",
            key=key,
            task_type=task_type,
            workspace_path=workspace_path,
        )
        return True

    def get(self, key: str) -> Optional[TaskInfo]:
        """Get task info by key."""
        info = self._tasks.get(key)
        if info and info.task.done():
            del self._tasks[key]
            return None
        return info

    async def cancel(self, key: str) -> bool:
        """Signal cancellation for a task, wherever it runs.

        Local first (the handle is here); otherwise broadcast to the owning
        replica. True means cancelled locally or a cancel request reached at
        least one listener.
        """
        try:
            return await self._get_registry().cancel(key)
        except Exception:
            return self._cancel_local(key)

    def _cancel_local(self, key: str) -> bool:
        """Cancel on THIS replica. False when it does not own the task."""
        info = self._tasks.get(key)
        if info and not info.task.done():
            info.circuit_breaker.set()
            logger.info("task_cancel_signaled", key=key, task_type=info.task_type)
            return True
        return False

    async def remove(self, key: str) -> None:
        """Remove a task entry, local and distributed."""
        self._tasks.pop(key, None)
        try:
            await self._get_registry().unregister(key)
        except Exception:
            pass  # TTL reclaims the key if unregister cannot reach Redis

    def _cleanup_done(self) -> None:
        """Remove completed tasks from tracker."""
        done_keys = [k for k, t in self._tasks.items() if t.task.done()]
        for k in done_keys:
            del self._tasks[k]


# Singleton
_tracker: Optional[TaskTracker] = None


def get_task_tracker() -> TaskTracker:
    """Get or create the global TaskTracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = TaskTracker()
    return _tracker
