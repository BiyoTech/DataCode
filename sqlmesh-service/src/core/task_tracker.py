"""Task tracker for managing async background operations.

Tracks running plan/apply tasks per workspace to:
- Prevent conflicting concurrent operations
- Report running status via /meta endpoint
- Enable cancellation via circuit breaker
"""

import asyncio
import time
from typing import Any, Dict, Optional

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


class TaskTracker:
    """Tracks running async tasks (plan/apply) across workspaces."""

    def __init__(self):
        # Key: "{workspace_path}:{gateway}" → TaskInfo
        self._tasks: Dict[str, TaskInfo] = {}

    def has_running_task(self, workspace_path: str = "") -> bool:
        """Check if any task is running.

        Args:
            workspace_path: If provided, check only for that workspace.
                           If empty, check globally.
        """
        if not workspace_path:
            return any(not t.task.done() for t in self._tasks.values())

        return any(
            not t.task.done()
            for key, t in self._tasks.items()
            if t.workspace_path == workspace_path
        )

    def register(
        self,
        key: str,
        workspace_path: str,
        task_type: str,
        task: asyncio.Task,
        circuit_breaker: asyncio.Event,
    ) -> None:
        """Register a new background task."""
        # Clean up completed tasks
        self._cleanup_done()

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

    def get(self, key: str) -> Optional[TaskInfo]:
        """Get task info by key."""
        info = self._tasks.get(key)
        if info and info.task.done():
            del self._tasks[key]
            return None
        return info

    def cancel(self, key: str) -> bool:
        """Signal cancellation for a task.

        Returns True if a running task was found and signaled.
        """
        info = self._tasks.get(key)
        if info and not info.task.done():
            info.circuit_breaker.set()
            logger.info("task_cancel_signaled", key=key, task_type=info.task_type)
            return True
        return False

    def remove(self, key: str) -> None:
        """Remove a task entry."""
        self._tasks.pop(key, None)

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
