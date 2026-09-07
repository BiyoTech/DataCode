"""Port segment allocation for multi-replica dagster-service.

## The problem

``_allocate_port`` used to hand out ports sequentially from one shared
``code_location_ports.json``. With N replicas sharing a filesystem (a single
host running two processes, or K8s pods over a shared PV) that is a
read-modify-write race: two replicas can read the same "taken" set, pick the
same port, and both spawn a gRPC server on it — one of them dies on bind, the
other silently serves the wrong location's code.

## The mechanism

Each replica claims an exclusive PORT SEGMENT — a contiguous slice of the port
space (``base + segment * size`` .. ``base + (segment+1) * size - 1``) — and
allocates only inside it:

* Segments are claimed in Redis with ``SET NX EX`` (value = replica id). The
  TTL is the crash-recovery bound, renewed by a background heartbeat; a replica
  that dies loses its lease and the segment is claimable again.
* A restarted replica first RECLAIMS the segment its key still holds (the value
  matches its stable replica id), so its code locations keep their ports
  across restarts — a port that silently moves invalidates workspace.yaml,
  remote registrations and operator notes (see code_location_manager).
* Without Redis the segment degrades to a stable hash of the replica id. Two
  replicas may then collide (same hash bucket); the allocator logs a warning
  and the platform's documented degradation applies: cross-replica coordination
  narrows to best-effort while Redis is unavailable.

Per-replica port assignments are persisted to a per-replica SHARD file
(``code_location_ports.{replica_id}.json``), so replicas sharing a directory
never write the same file and the write race disappears without a distributed
lock or a leader-elected single writer.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from src.config import Settings, settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Lease TTL and renewal cadence. The TTL is the crash-recovery bound: how long
# a dead replica's segment stays reserved after its last heartbeat. Renewal at
# TTL/3 tolerates two missed heartbeats before the lease lapses.
SEGMENT_LEASE_TTL_SECONDS = 90
SEGMENT_RENEW_INTERVAL_SECONDS = SEGMENT_LEASE_TTL_SECONDS // 3

# Release only our own segment. A plain DEL would let a restarted replica
# delete a lease a successor legitimately re-claimed while it was down.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _sanitize_replica_id(replica_id: str) -> str:
    """Make a replica id safe to embed in a filename.

    Container names and pod names are already safe; this guards the
    ``DATACODE_REPLICA_ID`` env var, which nothing constrains.
    """
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in replica_id)


@dataclass(frozen=True)
class SegmentLease:
    """The outcome of one allocation attempt."""

    segment: int
    # "redis" = exclusively leased in Redis; "hash" = degraded stable hash.
    mode: str

    @property
    def exclusive(self) -> bool:
        return self.mode == "redis"


class PortSegmentAllocator:
    """Claims and renews one port segment per replica.

    Lifecycle: :meth:`acquire` is idempotent and lazily starts the heartbeat
    task; :meth:`release` stops it and frees the Redis lease (process shutdown).
    The allocator never raises: a Redis outage degrades to hash mode.
    """

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        *,
        replica_id: Optional[str] = None,
        lease_ttl_seconds: int = SEGMENT_LEASE_TTL_SECONDS,
    ) -> None:
        self._settings = cfg or settings
        self._replica_id = replica_id
        self._lease_ttl = lease_ttl_seconds
        self._lease: Optional[SegmentLease] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_degrade_warn = 0.0

    # --- Public API ----------------------------------------------------------

    @property
    def replica_id(self) -> str:
        if self._replica_id is None:
            from src.codeloc.manager import get_code_location_manager

            self._replica_id = get_code_location_manager().replica_id()
        return self._replica_id

    @property
    def lease(self) -> Optional[SegmentLease]:
        return self._lease

    @property
    def shard_file_name(self) -> str:
        """Per-replica port persistence file (unique per replica)."""
        return f"code_location_ports.{_sanitize_replica_id(self.replica_id)}.json"

    async def acquire(self) -> SegmentLease:
        """Return this replica's segment, claiming/reclaiming it once.

        Idempotent: the first call does the work, later calls return the cached
        lease. Never raises — Redis failures fall back to hash mode so the
        service boots.
        """
        if self._lease is not None:
            return self._lease
        lease = await self._acquire_redis() or self._acquire_hash()
        self._lease = lease
        if lease.exclusive:
            self._start_heartbeat()
        logger.info(
            "port_segment_acquired",
            replica=self.replica_id,
            segment=lease.segment,
            mode=lease.mode,
            port_range=f"{self.segment_port_range(lease.segment)}",
        )
        return lease

    async def release(self) -> None:
        """Stop the heartbeat and free the Redis lease (shutdown path)."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._lease is not None and self._lease.exclusive:
            try:
                client = self._redis()
                if client is not None:
                    await client.eval(
                        _RELEASE_SCRIPT, 1, self._segment_key(self._lease.segment),
                        self.replica_id,
                    )
            except Exception as e:  # noqa: BLE001 - shutdown is best-effort
                logger.warning("port_segment_release_failed", error=str(e))
        self._lease = None

    def segment_port_range(self, segment: int) -> tuple[int, int]:
        """Inclusive [start, end] port range owned by ``segment``."""
        base = self._settings.code_location_port
        size = self._settings.code_location_port_segment_size
        start = base + segment * size
        return start, start + size - 1

    # --- Redis lease ---------------------------------------------------------

    def _segment_key(self, segment: int) -> str:
        from datacode_common.redis.client import redis_key

        return redis_key("ds", "port_segment", str(segment))

    async def _acquire_redis(self) -> Optional[SegmentLease]:
        """Claim a segment in Redis: reclaim ours, else ring-scan for a free one."""
        try:
            client = self._redis()
            if client is None:
                return None
            max_segments = self._settings.code_location_port_max_segments

            # 1. Reclaim: a restart wants its previous segment back so ports
            # stay stable. Scanning is bounded by max_segments (one SCAN).
            reclaimed = await self._reclaim(client)
            if reclaimed is not None:
                await client.expire(self._segment_key(reclaimed), self._lease_ttl)
                return SegmentLease(reclaimed, "redis")

            # 2. Ring-scan from the preferred (stable-hash) segment outward.
            preferred = self._preferred_segment()
            for offset in range(max_segments):
                segment = (preferred + offset) % max_segments
                created = await client.set(
                    self._segment_key(segment),
                    self.replica_id,
                    nx=True,
                    ex=self._lease_ttl,
                )
                if created:
                    return SegmentLease(segment, "redis")
            # All segments taken: the deployment has more replicas than the
            # configured maximum. Fall through to hash mode with a loud log —
            # hash mode may collide, but refusing to boot is worse.
            logger.error(
                "port_segment_exhausted",
                max_segments=max_segments,
                replica=self.replica_id,
            )
            return None
        except Exception as e:  # noqa: BLE001 - degradation, never fail boot
            self._warn_degraded("acquire", e)
            return None

    async def _reclaim(self, client) -> Optional[int]:
        """Find a segment key currently held by this replica id."""
        pattern = self._segment_key("*")
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
            for key in keys:
                holder = await client.get(key)
                if holder == self.replica_id:
                    # Key suffix is the segment number.
                    try:
                        return int(str(key).rsplit(":", 1)[-1])
                    except ValueError:
                        continue
            if cursor == 0:
                return None

    def _preferred_segment(self) -> int:
        """Stable hash segment: preferred start point, and the fallback mode."""
        digest = hashlib.sha256(self.replica_id.encode("utf-8")).hexdigest()
        return int(digest, 16) % self._settings.code_location_port_max_segments

    def _acquire_hash(self) -> SegmentLease:
        """Degraded mode: derive the segment deterministically, no exclusivity."""
        segment = self._preferred_segment()
        self._warn_degraded("hash_fallback", None, once=True)
        return SegmentLease(segment, "hash")

    def _start_heartbeat(self) -> None:
        """Renew the lease in the background for this process's lifetime."""

        async def _renew_loop() -> None:
            while True:
                await asyncio.sleep(SEGMENT_RENEW_INTERVAL_SECONDS)
                try:
                    client = self._redis()
                    if client is None:
                        return
                    assert self._lease is not None
                    await client.expire(
                        self._segment_key(self._lease.segment), self._lease_ttl
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - keep renewing on errors
                    self._warn_degraded("renew", e)
                    # Redis outage longer than the TTL lets another replica
                    # claim our segment; re-acquiring the lease re-establishes
                    # exclusivity (or documents its loss) when Redis returns.
                    lease = self._lease
                    self._lease = None
                    await self.acquire()
                    if self._lease is not None and lease is not None:
                        if self._lease.segment != lease.segment:
                            logger.warning(
                                "port_segment_changed_after_reacquire",
                                old_segment=lease.segment,
                                new_segment=self._lease.segment,
                            )

        self._heartbeat_task = asyncio.get_running_loop().create_task(
            _renew_loop(), name="port-segment-heartbeat"
        )

    def _redis(self):
        from datacode_common.redis.client import get_redis

        return get_redis()

    def _warn_degraded(self, op: str, exc: Optional[Exception], once: bool = False) -> None:
        """Rate-limited degradation warning (see run_registry for the pattern)."""
        now = time.monotonic()
        if now - self._last_degrade_warn < 60.0:
            return
        self._last_degrade_warn = now
        logger.warning(
            "port_segment_degraded",
            op=op,
            replica=self.replica_id,
            error=str(exc) if exc else None,
            once=once,
            detail=(
                "Redis unavailable: port segment falls back to a stable hash. "
                "Concurrent replicas may hash to the same segment and collide "
                "on ports while Redis is down."
            ),
        )
