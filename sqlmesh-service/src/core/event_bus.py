"""In-process event bus for SSE broadcasting.

Provides a pub/sub mechanism for real-time event delivery to connected
SSE clients. Matches SQLMesh web server's console event dispatch pattern.

Architecture:
- Single global EventBus instance
- Multiple SSE clients register async queues as subscribers
- Services publish events which get broadcast to all subscribers
- Each subscriber gets an independent copy to avoid blocking
"""

import asyncio
import enum
import time
from typing import Any, Dict, Optional, Set

from src.utils.logging import get_logger

logger = get_logger(__name__)


class EventTopic(str, enum.Enum):
    """SSE event topics matching SQLMesh web server conventions."""

    PLAN_OVERVIEW = "plan-overview"
    PLAN_APPLY = "plan-apply"
    PLAN_CANCEL = "plan-cancel"
    MODELS = "models"
    FILE = "file"
    ERRORS = "errors"
    META = "meta"
    PING = "ping"


class SSEEvent:
    """Structured event for SSE delivery."""

    __slots__ = ("topic", "data", "workspace_path", "timestamp")

    def __init__(
        self,
        topic: EventTopic,
        data: Any = None,
        workspace_path: str = "",
    ):
        self.topic = topic
        self.data = data
        self.workspace_path = workspace_path
        self.timestamp = time.time()


class EventBus:
    """Broadcast event bus for SSE subscribers.

    Thread-safe: uses asyncio.Queue per subscriber for non-blocking delivery.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._global_subscribers: Set[asyncio.Queue] = set()

    def subscribe(self, workspace_path: str = "") -> asyncio.Queue:
        """Register a new subscriber queue.

        Args:
            workspace_path: If provided, only receives events for that workspace.
                           Empty string means receive all events.

        Returns:
            An asyncio.Queue that will receive SSEEvent instances.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        if workspace_path:
            if workspace_path not in self._subscribers:
                self._subscribers[workspace_path] = set()
            self._subscribers[workspace_path].add(queue)
        else:
            self._global_subscribers.add(queue)

        logger.debug(
            "sse_subscriber_added",
            workspace_path=workspace_path or "(global)",
            total_subscribers=self._subscriber_count(),
        )
        return queue

    def unsubscribe(self, queue: asyncio.Queue, workspace_path: str = "") -> None:
        """Remove a subscriber queue."""
        if workspace_path:
            subs = self._subscribers.get(workspace_path)
            if subs:
                subs.discard(queue)
                if not subs:
                    del self._subscribers[workspace_path]
        else:
            self._global_subscribers.discard(queue)

        logger.debug(
            "sse_subscriber_removed",
            workspace_path=workspace_path or "(global)",
            total_subscribers=self._subscriber_count(),
        )

    async def publish(self, event: SSEEvent) -> None:
        """Broadcast event to all matching subscribers (non-blocking).

        Delivers to:
        - All global subscribers
        - Workspace-specific subscribers matching event.workspace_path
        """
        targets: Set[asyncio.Queue] = set(self._global_subscribers)

        if event.workspace_path and event.workspace_path in self._subscribers:
            targets.update(self._subscribers[event.workspace_path])

        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest event and retry to prevent blocking
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _subscriber_count(self) -> int:
        count = len(self._global_subscribers)
        for subs in self._subscribers.values():
            count += len(subs)
        return count


# Singleton
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Get or create the global EventBus instance."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
