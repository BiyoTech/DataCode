"""Per-session event bus using asyncio.Queue.

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
"""

import asyncio
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


@dataclass
class _SessionQueue:
    """Internal state for a single session's event queue.

    `owner` is the token of the SSE connection currently consuming this queue.
    `None` means the queue is a buffer with no consumer attached yet (created
    lazily by publish()/POST chat before the SSE client connects).
    """

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    active: bool = True
    owner: t.Optional[str] = None


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
        """
        sq = self._ensure_queue(session_id)
        if not sq.active:
            return
        try:
            sq.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "event_queue_full_dropping",
                session_id=session_id,
                event_type=event.type.value,
            )

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
            new_sq.queue.put_nowait(item)
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
