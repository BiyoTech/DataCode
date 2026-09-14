"""Event bus for SSE broadcasting: local queues + Redis fan-out.

Provides a pub/sub mechanism for real-time event delivery to connected
SSE clients. Matches SQLMesh web server's console event dispatch pattern.

Architecture:
- Single global EventBus instance per process
- Multiple SSE clients register async queues as subscribers
- Services publish events which get broadcast to all subscribers
- Each subscriber gets an independent copy to avoid blocking

Cross-replica fan-out:
  A plan/apply may run on one replica while the SSE client watches another.
  publish() therefore also broadcasts to a Redis channel per workspace, and a
  process-wide pump (one subscription connection, pattern-matched) feeds
  messages from OTHER replicas into the local subscriber set. Messages carry
  the producing replica's id and are dropped when they originated here — the
  local delivery already handled those, and without the filter every event
  would be delivered twice.

  Delivery is at-most-once across replicas: Pub/Sub does not replay what was
  published while a pump was (re)connecting. Clients reconnect and refill from
  persisted state (task status, plan history); the stream is a live view, not
  the record.

  Without Redis the bus degrades to the previous in-process behaviour.
"""

import asyncio
import enum
import json
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
    # Lease lifecycle (design 03 §4.3): the frontend subscribes so a drain
    # surfaces as "your working copy is moving" instead of as the next
    # request's failure.
    WORKSPACE = "workspace"
    # Code-location reload progress: stage transitions plus the installer's
    # output, line by line. A reload can take minutes (a first dependency
    # install), so it is an asynchronous job whose progress arrives here while
    # the authoritative state stays readable from its status endpoint.
    DEPLOYMENT = "deployment"


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

    def to_json(self) -> str:
        """Serialize for the Redis fan-out.

        Raises TypeError when ``data`` is not JSON-serializable; the caller
        treats that as "this event is local-only".
        """
        return json.dumps(
            {
                "topic": self.topic.value,
                "data": self.data,
                "workspace_path": self.workspace_path,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload) -> "SSEEvent":
        """Rebuild an event from its Redis fan-out form.

        Accepts the event as a nested object (the envelope embeds it parsed)
        or as a JSON string.
        """
        obj = payload if isinstance(payload, dict) else json.loads(payload)
        return cls(
            topic=EventTopic(obj["topic"]),
            data=obj.get("data"),
            workspace_path=obj.get("workspace_path", ""),
        )


# Channel segment for events with no workspace scope (global subscribers).
_GLOBAL_CHANNEL = "__global__"


def _channel_for(workspace_path: str) -> str:
    from datacode_common.redis.client import redis_config

    scope = workspace_path if workspace_path else _GLOBAL_CHANNEL
    return f"{redis_config().key_prefix}:sm:sse:{scope}"


class _RedisFanout:
    """Process-wide bridge between Redis Pub/Sub and the local subscriber set.

    One pattern subscription covers every workspace channel — a connection
    per SSE stream would exhaust the client pool. Started lazily on the first
    subscriber and never stopped: an idle pub/sub connection costs one socket,
    while start/stop churn risks the message loss this exists to prevent.
    """

    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._pump = None
        self._replica_id: Optional[str] = None

    def _ensure_replica_id(self) -> str:
        """This fan-out's producer identity (instance-level for testability)."""
        if self._replica_id is None:
            from datacode_common.redis import replica_id

            self._replica_id = replica_id()
        return self._replica_id

    def ensure_started(self) -> None:
        """Start the pump if Redis is configured (fire-and-forget).

        Called from synchronous contexts (subscribe), hence the task spawn.
        """
        if self._pump is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._start())

    async def _start(self) -> None:
        from datacode_common.redis import PatternPump, get_redis
        from datacode_common.redis.client import redis_config

        if get_redis() is None:
            logger.info("event_bus_local_only", reason="platform_redis_disabled")
            return
        # _replica_id stays None here: _ensure_replica_id() resolves it lazily
        # so a pre-set identity (tests inject one to simulate a second
        # replica) is not clobbered.
        self._pump = PatternPump(
            domain="sm-sse",
            pattern=f"{redis_config().key_prefix}:sm:sse:*",
            on_message=self._on_message,
        )
        await self._pump.start()
        logger.info("event_bus_redis_fanout_started")

    async def publish(self, event: "SSEEvent") -> None:
        """Broadcast to subscribers on other replicas; failures are local-only.

        The local delivery has already happened when this runs — a Redis hiccup
        must not fail the operation that produced the event. Publishing needs
        no subscription of our own: a replica with zero local SSE clients is
        still a producer for clients connected elsewhere.
        """
        try:
            from datacode_common.redis import get_redis

            client = get_redis()
            if client is None:
                return
            await client.publish(
                _channel_for(event.workspace_path),
                json.dumps(
                    {"origin": self._ensure_replica_id(), "event": json.loads(event.to_json())},
                    ensure_ascii=False,
                ),
            )
        except TypeError:
            # Non-JSON-serializable payload: local subscribers still get it.
            logger.debug(
                "event_bus_fanout_skip_non_serializable",
                topic=event.topic.value,
            )
        except Exception as exc:
            logger.debug(
                "event_bus_fanout_publish_failed",
                topic=event.topic.value,
                error=str(exc),
            )

    async def _on_message(self, channel: str, payload: str) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            logger.warning("event_bus_malformed_message", channel=channel)
            return
        if message.get("origin") == self._ensure_replica_id():
            return  # local delivery already handled this event

        try:
            event = SSEEvent.from_json(message.get("event", ""))
        except (ValueError, KeyError, TypeError) as exc:
            # Unknown topic: this replica is older than the publisher.
            logger.debug(
                "event_bus_remote_event_dropped",
                channel=channel,
                error=str(exc),
            )
            return
        self._bus.deliver_local(event)


class EventBus:
    """Broadcast event bus for SSE subscribers.

    Thread-safe: uses asyncio.Queue per subscriber for non-blocking delivery.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._global_subscribers: Set[asyncio.Queue] = set()
        self._fanout = _RedisFanout(self)

    def subscribe(self, workspace_path: str = "") -> asyncio.Queue:
        """Register a new subscriber queue.

        Args:
            workspace_path: If provided, only receives events for that workspace.
                           Empty string means receive all events.

        Returns:
            An asyncio.Queue that will receive SSEEvent instances.
        """
        self._fanout.ensure_started()
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
        """Broadcast event to all matching subscribers, local and remote.

        Delivers to:
        - All global subscribers (this replica)
        - Workspace-specific subscribers matching event.workspace_path
        - The Redis channel, for subscribers on other replicas
        """
        self.deliver_local(event)
        await self._fanout.publish(event)

    def deliver_local(self, event: SSEEvent) -> None:
        """Deliver to this replica's subscribers only (no re-broadcast).

        Also the entry point for events arriving from the Redis pump —
        delivering those via publish() would ping-pong them back to Redis.
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
