"""Runtime heartbeat — how this instance becomes placeable (design 03 §3.3).

The controller places a new lease on the emptiest instance, so it needs each
instance's current load. That report comes from the instance itself rather than
being computed centrally, because the real constraint is this pod's memory
budget against the size of the repositories it happens to hold: a controller
counting leases would place a 2000-model repo as if it cost what a 50-model one
does (design 07 Q3).

Two Redis objects, with different lifetimes on purpose
------------------------------------------------------
* ``rt:{runtime_id}`` — the heartbeat itself, written with a TTL of a few
  intervals. It EXPIRES, which is what makes a crashed instance stop being a
  placement candidate without anyone running a reaper.
* ``rt:index`` — a set of live ids. A set cannot expire per-member, so stale
  ids are pruned when a placement read finds no heartbeat behind them. The
  index exists because placement runs on the request path (on a lease miss) and
  ``SCAN rt:*`` costs time proportional to the whole keyspace rather than to
  the number of runtimes.

Reporting failures are never fatal: an instance that cannot publish its
heartbeat keeps serving the leases it already holds — it simply stops receiving
new ones, which is the correct degradation.
"""

from __future__ import annotations

import asyncio
import math
import typing as t
from datetime import datetime, timezone

from datacode_common.lease import (
    RuntimeHeartbeat,
    encode_runtime_heartbeat,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Publish this often. Short enough that a controller decision is based on
# recent load, long enough that 33 instances cost a trivial write rate.
HEARTBEAT_INTERVAL_SECONDS = 10.0

# Survive this many missed intervals before disappearing from placement. Two
# would evict an instance for a single slow write; the point of the TTL is to
# detect death, not slowness.
HEARTBEAT_TTL_INTERVALS = 3


class HeartbeatReporter:
    """Publishes this instance's capacity, and prunes dead peers on read."""

    def __init__(
        self,
        *,
        client,
        runtime_id: str,
        addr: str,
        max_leases: int,
        load_provider: t.Callable[[], tuple[int, float, bool, tuple[str, ...]]],
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._runtime_id = runtime_id
        self._addr = addr
        self._max_leases = max_leases
        # Injected rather than read from a global: the holder owns the lease
        # count and the process owns the memory figure, and a reporter that
        # reached into either would be untestable without both.
        self._load_provider = load_provider
        self._interval = float(interval_seconds)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Begin publishing. Idempotent."""
        if self._task is not None:
            return
        # Publish once synchronously so this instance is placeable immediately
        # rather than one interval after startup.
        await self.publish_once()
        self._task = asyncio.create_task(self._loop(), name="lease-heartbeat")
        logger.info(
            "heartbeat_started", runtime_id=self._runtime_id, interval=self._interval
        )

    async def stop(self) -> None:
        """Stop publishing and remove this instance from placement.

        Explicit removal on shutdown, rather than waiting for the TTL: a
        graceful drain should stop attracting leases at once, not in 30 seconds.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        try:
            await self._client.delete(runtime_redis_key(self._runtime_id))
            await self._client.srem(runtime_index_redis_key(), self._runtime_id)
        except Exception as exc:  # RedisError / OSError
            # The TTL is the backstop; a failed cleanup only delays it.
            logger.warning("heartbeat_cleanup_failed", error=str(exc))
        logger.info("heartbeat_stopped", runtime_id=self._runtime_id)

    def current(self) -> RuntimeHeartbeat:
        """This instance's heartbeat as it would be published right now."""
        lease_count, memory_ratio, draining, leases = self._load_provider()
        return RuntimeHeartbeat(
            runtime_id=self._runtime_id,
            addr=self._addr,
            lease_count=lease_count,
            max_leases=self._max_leases,
            memory_used_ratio=memory_ratio,
            reported_at=datetime.now(timezone.utc),
            draining=draining,
            leases=leases,
        )

    async def publish_once(self) -> bool:
        """Publish one heartbeat. Returns False when Redis refused it."""
        heartbeat = self.current()
        payload = encode_runtime_heartbeat(heartbeat)
        # Round UP and floor at 1s: int() truncation would yield ex=0 for a
        # sub-second interval, and Redis treats a 0 TTL as "already expired",
        # so the instance would publish a heartbeat that never exists. Silent
        # un-placeability is far worse than a slightly generous TTL.
        ttl_seconds = max(1, math.ceil(self._interval * HEARTBEAT_TTL_INTERVALS))
        try:
            await self._client.set(
                runtime_redis_key(self._runtime_id), payload, ex=ttl_seconds
            )
            await self._client.sadd(runtime_index_redis_key(), self._runtime_id)
        except Exception as exc:  # RedisError / OSError
            # Degradation, not failure: leases already held keep being served.
            logger.warning(
                "heartbeat_publish_failed",
                runtime_id=self._runtime_id,
                error=str(exc),
            )
            return False
        return True

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.publish_once()
