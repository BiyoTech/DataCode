"""Per-session event bus: local asyncio.Queue + Redis fan-out.

Decouples Agent execution (producer) from the SSE endpoint (consumer).
Provides an async publish/subscribe interface with heartbeat injection and a
strict single-owner subscription model.

Ownership model (why it matters):
  asyncio.Queue delivers each item to exactly ONE waiter — reads are
  destructive. If two SSE connections ever consumed the same queue at once
  (e.g. during a client reconnect while the previous subscriber is still being
  torn down), streamed events would be split ~50/50 between them, so half the
  message deltas would vanish into the dead connection and the terminal
  agent.finish could be lost too. To prevent this, every SSE connection is
  handed an exclusively-owned queue via `attach_subscriber`, and a reconnect
  atomically takes over — migrating buffered events and retiring the old queue
  so its stale consumer exits immediately. There is never more than one live
  consumer per session queue.

Cross-replica fan-out:
  POST /chat may land on one replica while the SSE stream is connected to
  another. A process-wide Redis Pub/Sub pump (one subscription connection for
  ALL sessions) bridges that: publish() writes locally AND to the session's
  channel; the pump feeds messages from OTHER replicas into the local queue.
  Messages carry the producing replica's id and are dropped when they
  originated here (the local write already delivered them) — without that
  filter every subscriber would see each local event twice.

  Delivery is at-most-once across replicas by design: Pub/Sub does not replay
  what was published while a pump was (re)connecting. The platform contract is
  that clients reconnect and refill from the persisted timeline, so a missed
  delta heals on the next interaction rather than being lost forever.

  Without Redis everything degrades to the previous single-replica behaviour.
"""

import asyncio
import json
import typing as t
import uuid
from dataclasses import dataclass, field

from src.protocol.messages import ACPEvent
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Sentinel value to signal stream termination.
_STREAM_END = object()

# Heartbeat interval in seconds.
HEARTBEAT_INTERVAL = 30

# Bound the queue of a session with a live subscriber. Local producers are
# well-behaved, but a Redis pump flooding a slow consumer (client stopped
# reading while the server keeps publishing) should be dropped-with-log rather
# than allowed to grow without bound.
_QUEUE_MAXSIZE = 2048


@dataclass
class _SessionQueue:
    """Internal state for a single session's event queue.

    `owner` is the token of the SSE connection currently consuming this queue.
    `None` means the queue is a buffer with no consumer attached yet (created
    lazily by publish()/POST chat before the SSE client connects).
    """

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAXSIZE))
    active: bool = True
    owner: t.Optional[str] = None


class _RedisFanout:
    """Process-wide bridge between Redis Pub/Sub and the local queues.

    One subscription connection covers every session (pattern subscription) —
    a connection per SSE stream would exhaust the client pool under normal
    concurrency. Started lazily on the first subscriber and never stopped:
    an idle pub/sub connection costs one socket, while start/stop churn around
    reconnects risks exactly the message loss this exists to prevent.
    """

    def __init__(self, bus: "SessionEventBus") -> None:
        self._bus = bus
        self._pump = None  # built lazily; None until Redis is proven available
        self._replica_id = None

    def _ensure_replica_id(self) -> str:
        """This fan-out's producer identity (instance-level for testability)."""
        if self._replica_id is None:
            from datacode_common.redis import replica_id

            self._replica_id = replica_id()
        return self._replica_id

    def _ensure_started(self) -> None:
        """Start the pump if Redis is configured (fire-and-forget).

        Called from synchronous contexts (attach_subscriber), hence the task
        spawn. Errors surface through the pump's own logging; a Redis outage
        parks the pump in its reconnect loop instead of failing callers.
        """
        if self._pump is not None:
            return
        asyncio.get_running_loop().create_task(self._start())

    async def _start(self) -> None:
        from datacode_common.redis import PatternPump, get_redis, redis_config

        if get_redis() is None:
            logger.info("session_event_bus_local_only", reason="platform_redis_disabled")
            return
        cfg = redis_config()
        channel_prefix = f"{cfg.key_prefix}:agent:session"
        # _replica_id stays None here: _ensure_replica_id() resolves it lazily
        # so a pre-set identity (tests inject one to simulate a second
        # replica) is not clobbered.
        self._pump = PatternPump(
            domain="agent-sse",
            pattern=f"{channel_prefix}:*",
            on_message=self._on_message,
        )
        await self._pump.start()
        logger.info("session_event_bus_redis_fanout_started")

    async def publish(self, session_id: str, event: ACPEvent) -> None:
        """Broadcast an event to subscribers on other replicas.

        Failures are logged at debug and swallowed: this is the cross-replica
        half of a delivery whose local half has already succeeded, and a Redis
        hiccup must not fail the agent run that produced the event. Publishing
        needs no subscription of our own: a replica with zero local SSE
        clients is still a producer for clients connected elsewhere.
        """
        try:
            from datacode_common.redis import get_redis

            client = get_redis()
            if client is None:
                return
            envelope = json.dumps(
                # event.to_json() yields a string; embedding it verbatim would
                # double-escape. Parse it back so the envelope carries the
                # event as a nested object.
                {"origin": self._ensure_replica_id(), "event": json.loads(event.to_json())},
                ensure_ascii=False,
            )
            await client.publish(
                self._channel(session_id), envelope
            )
        except Exception as exc:
            logger.debug(
                "session_event_bus_fanout_publish_failed",
                session_id=session_id,
                error=str(exc),
            )

    def _channel(self, session_id: str) -> str:
        from datacode_common.redis.client import redis_config

        return f"{redis_config().key_prefix}:agent:session:{session_id}:events"

    def _session_of(self, channel: str) -> t.Optional[str]:
        """Extract the session id from '<prefix>:agent:session:<id>:events'."""
        marker = ":agent:session:"
        idx = channel.rfind(marker)
        if idx < 0:
            return None
        rest = channel[idx + len(marker):]
        if not rest.endswith(":events"):
            return None
        return rest[: -len(":events")]

    async def _on_message(self, channel: str, payload: str) -> None:
        """Deliver a remote event into the session's local queue."""
        try:
            message = json.loads(payload)
        except ValueError:
            logger.warning("session_event_bus_malformed_message", channel=channel)
            return
        if message.get("origin") == self._ensure_replica_id():
            return  # our own local write already delivered this event

        session_id = self._session_of(channel)
        if session_id is None:
            return
        try:
            event = ACPEvent.from_json(message.get("event", ""))
        except (ValueError, KeyError, TypeError) as exc:
            # Unknown event type: this replica is older than the publisher.
            # The event still reaches its client through the producing replica.
            logger.debug(
                "session_event_bus_remote_event_dropped",
                session_id=session_id,
                error=str(exc),
            )
            return

        sq = self._bus._queues.get(session_id)
        if sq is None or not sq.active:
            # No local subscriber: the event's audience is elsewhere. History
            # is served from the persisted timeline, so buffering is pointless.
            return
        try:
            sq.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "event_queue_full_dropping",
                session_id=session_id,
                event_type=event.type.value,
                source="redis",
            )


class SessionEventBus:
    """Per-session event queue for SSE streaming with a single-owner model.

    Lifecycle:
      1. publish() lazily creates a buffer queue if the SSE client has not
         connected yet, so no events are lost in the POST-chat -> SSE-connect gap.
      2. attach_subscriber() gives an SSE connection an exclusively-owned queue.
      3. subscribe() yields events for that owned queue until it is retired.
      4. detach() releases the queue only if this connection still owns it.
    """

    def __init__(self):
        self._queues: t.Dict[str, _SessionQueue] = {}
        self._fanout = _RedisFanout(self)

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    def _ensure_queue(self, session_id: str) -> _SessionQueue:
        """Lazily create a buffer queue if one does not exist.

        The queue buffers events until an SSE client connects, preventing loss
        during the race window between POST /chat and SSE connect.
        """
        sq = self._queues.get(session_id)
        if sq is None:
            sq = _SessionQueue()
            self._queues[session_id] = sq
            logger.info("lazy_queue_created", session_id=session_id)
        return sq

    def ensure_queue(self, session_id: str) -> None:
        """Public helper: guarantee a buffer queue exists without attaching a
        consumer. Never replaces an existing queue."""
        self._ensure_queue(session_id)

    async def publish(self, session_id: str, event: ACPEvent) -> None:
        """Push an event to the session's current queue.

        Always targets the queue currently owned by the live subscriber (or the
        lazy buffer queue), so events never race across a reconnect takeover.
        The event is also fanned out to other replicas' subscribers.
        """
        sq = self._ensure_queue(session_id)
        if sq.active:
            try:
                sq.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "event_queue_full_dropping",
                    session_id=session_id,
                    event_type=event.type.value,
                )
        await self._fanout.publish(session_id, event)

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    def attach_subscriber(self, session_id: str) -> t.Tuple[_SessionQueue, str]:
        """Attach an SSE consumer and return an exclusively-owned queue + token.

        This method performs no `await`, so it runs atomically within the
        asyncio event loop, guaranteeing a single consumer per queue:
          - No queue yet        -> create one owned by this connection.
          - Buffer / orphan queue (owner is None) -> adopt it, keeping buffered
            events.
          - Queue with a live owner (reconnect / extra tab) -> take over: spin
            up a fresh queue, migrate buffered events, and retire the old queue
            so its stale consumer exits on its next loop iteration.
        """
        self._fanout._ensure_started()
        token = str(uuid.uuid4())
        existing = self._queues.get(session_id)

        if existing is None:
            sq = _SessionQueue(owner=token)
            self._queues[session_id] = sq
            logger.info("subscriber_attached", session_id=session_id, mode="new")
            return sq, token

        if existing.owner is None:
            # Adopt the buffered queue — preserve its pending events.
            existing.owner = token
            existing.active = True
            logger.info("subscriber_attached", session_id=session_id, mode="adopt")
            return existing, token

        # Take over from a live/stale consumer. Migrate any buffered events so
        # the new connection loses nothing already queued.
        pending: t.List[t.Any] = []
        while not existing.queue.empty():
            try:
                item = existing.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not _STREAM_END:
                pending.append(item)

        new_sq = _SessionQueue(owner=token)
        for item in pending:
            try:
                new_sq.queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        self._queues[session_id] = new_sq

        # Retire the old queue and wake its consumer so it exits its get().
        existing.active = False
        try:
            existing.queue.put_nowait(_STREAM_END)
        except asyncio.QueueFull:
            pass
        logger.info(
            "subscriber_taken_over",
            session_id=session_id,
            migrated_events=len(pending),
        )
        return new_sq, token

    async def subscribe(self, sq: _SessionQueue) -> t.AsyncGenerator[ACPEvent, None]:
        """Yield events for an exclusively-owned queue.

        Injects a heartbeat signal (yields None) every HEARTBEAT_INTERVAL
        seconds while idle. Terminates when the queue is retired (active=False)
        or the _STREAM_END sentinel is received.
        """
        while sq.active:
            try:
                item = await asyncio.wait_for(sq.queue.get(), timeout=HEARTBEAT_INTERVAL)
                if item is _STREAM_END:
                    break
                yield item
            except asyncio.TimeoutError:
                # Idle timeout — emit a heartbeat unless the queue was retired.
                if not sq.active:
                    break
                yield None  # type: ignore[misc]

    def detach(self, session_id: str, token: str) -> None:
        """Release the queue only if this connection still owns it.

        No-op when a newer connection has already taken over (token mismatch),
        so we never destroy the queue out from under the current consumer.
        """
        sq = self._queues.get(session_id)
        if sq is not None and sq.owner == token:
            sq.active = False
            self._queues.pop(session_id, None)
            logger.info("subscriber_detached", session_id=session_id)

    def has_subscriber(self, session_id: str) -> bool:
        """Check if a session has a live SSE consumer attached."""
        sq = self._queues.get(session_id)
        return sq is not None and sq.active and sq.owner is not None


# Global singleton
event_bus = SessionEventBus()
