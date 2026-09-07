"""Pattern-subscription pump: Redis Pub/Sub → asyncio callback.

The platform's SSE streams are fed by per-replica asyncio queues today, which
means a subscriber connected to replica B never sees events produced on
replica A. Redis Pub/Sub is the transport that fixes that, and this module is
the one way services consume it, so the hard parts are solved once:

* **One pump per pattern per loop.** A pub/sub connection is a dedicated Redis
  connection; one per channel would multiply connections for no benefit.
* **Automatic reconnection with backoff.** Pub/Sub has no delivery guarantees
  across a reconnect: messages published while disconnected are simply gone.
  That is an accepted trade-off — the platform's contract (see the production
  architecture doc, SSE reconnection) is that clients reconnect and refill
  from persisted state (the agent timeline / task status tables), not that the
  transport replays history.
* **Silent degradation.** A pump whose Redis is down parks itself and retries
  with backoff; it never raises into its host service. Without Redis the
  streams keep working as single-replica streams.

Usage::

    pump = PatternPump("agent", pattern="session:*:events",
                       on_message=handle)
    await pump.start()          # background task; safe to call again
    ...
    await pump.stop()

The payload is JSON with a well-known envelope: {"origin": ..., "kind": ...,
...}. Callers compare ``origin`` to :func:`replica_id` and drop their own
messages — publishers always also deliver locally, so without that filter
every subscriber would see each local event twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import Awaitable, Callable, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from datacode_common.redis.client import get_redis

logger = logging.getLogger(__name__)

# Reconnect backoff bounds. The floor is one second so a flapping Redis does
# not turn into a busy-loop; the ceiling bounds how long a recovered instance
# takes to be picked up again.
_RECONNECT_MIN_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0

# Replica identity: hostname + pid + per-boot suffix. The suffix exists
# because pid recycling on a reboot would otherwise let a restarted process be
# mistaken for a still-live one (both matter for origin filtering and for run
# registry ownership).
_BOOT_SUFFIX = uuid.uuid4().hex[:8]
_replica_id: Optional[str] = None


def replica_id() -> str:
    """Return this process's stable replica identifier."""
    global _replica_id
    if _replica_id is None:
        _replica_id = f"{socket.gethostname()}/{os.getpid()}/{_BOOT_SUFFIX}"
    return _replica_id


class PatternPump:
    """Fan messages matching a pattern into an asyncio callback.

    The pump owns a background task that holds one Redis pub/sub connection.
    ``on_message(channel, payload)`` is awaited on the running loop for every
    message; exceptions from the callback are logged and never kill the pump.
    """

    def __init__(
        self,
        *,
        domain: str,
        pattern: str,
        on_message: Callable[[str, str], Awaitable[None]],
        client: Optional[Redis] = None,
    ) -> None:
        self._domain = domain
        self._pattern = pattern
        self._on_message = on_message
        self._client = client
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        """Whether the pump task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start pumping (no-op if already running).

        Resolves the Redis client lazily: a pump created while Redis is
        disabled still starts — its loop will find no client and park — so
        callers need no conditional wiring of their own.
        """
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"redis-pump-{self._domain}"
        )
        logger.debug(
            "pubsub_pump_started domain=%s pattern=%s",
            self._domain,
            self._pattern,
        )

    async def stop(self) -> None:
        """Stop pumping and release the subscription."""
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.debug("pubsub_pump_stopped domain=%s", self._domain)

    async def _run(self) -> None:
        """Subscribe forever, reconnecting with capped backoff."""
        backoff = _RECONNECT_MIN_SECONDS
        while not self._stopping.is_set():
            client = self._client or get_redis()
            if client is None:
                # Redis disabled at the platform level: nothing to pump. Park
                # until stop() rather than spinning.
                await self._park_until_stop()
                return

            try:
                async with client.pubsub() as pubsub:
                    await pubsub.psubscribe(self._pattern)
                    logger.info(
                        "pubsub_pump_subscribed domain=%s pattern=%s",
                        self._domain,
                        self._pattern,
                    )
                    backoff = _RECONNECT_MIN_SECONDS
                    async for message in pubsub.listen():
                        if self._stopping.is_set():
                            return
                        if message.get("type") != "pmessage":
                            continue  # subscribe confirmations etc.
                        channel = message.get("channel", "")
                        payload = message.get("data", "")
                        if not isinstance(channel, str) or not isinstance(payload, str):
                            continue  # decode_responses=True should guarantee str
                        await self._deliver(channel, payload)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                logger.warning(
                    "pubsub_pump_disconnected domain=%s error=%s retry_in=%.1fs",
                    self._domain,
                    exc,
                    backoff,
                )

            if self._stopping.is_set():
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                return  # stop() requested during the backoff
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    async def _deliver(self, channel: str, payload: str) -> None:
        """Invoke the callback, isolating it from the pump's lifecycle."""
        try:
            await self._on_message(channel, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A buggy callback must not take the subscription down with it.
            logger.exception(
                "pubsub_pump_callback_failed domain=%s channel=%s error=%s",
                self._domain,
                channel,
                exc,
            )

    async def _park_until_stop(self) -> None:
        logger.info("pubsub_pump_parked domain=%s reason=redis_disabled", self._domain)
        await self._stopping.wait()
