"""Lease-aware resolution for Python callers (design 03 §3.3, design 05 §5.2).

Why this exists in the shared library
-------------------------------------
The lease's guarantee is that exactly ONE workspace-runtime replica owns a
working copy, and it is worthless unless every caller honours it. The edge
gateway does (``gateway/internal/leaserouter``), but the gateway is not the only
caller: agent-service issues tool calls, queries and metadata reads carrying a
``workspace_path``, control-service dispatches every Git / engine side effect
over the env-control channel, and in Kubernetes ``services.runtime`` resolves to
a HEADLESS Service — its DNS returns every pod, so dialing it lands on an
arbitrary replica. With N replicas, N-1 of N such calls reach a replica that
does not hold the lease.

Two entry points, because callers hold different things
-------------------------------------------------------
:meth:`LeaseRouter.resolve` takes a ``workspace_path`` (what a tool call or a
file read carries) and :meth:`LeaseRouter.resolve_lease_key` takes the key
itself (what the control plane holds: it addresses working copies by logical
identifiers and owns no filesystem). Both run the SAME resolution — the path
form only derives the key first — so a caller cannot pick a different routing
policy by picking a different overload.

That failure is invisible in development (one replica always holds everything)
and near-total in production, which is why the resolution logic belongs here
rather than in one service: a second implementation of "which replica owns this"
is how a second writer eventually appears.

Resolution order, and why each step exists
------------------------------------------
 1. local cache (short TTL) — a coordination round trip per request would put
    Redis on the hot path of every file read;
 2. authoritative Redis lookup — the lease record says who owns the copy;
 3. placement — a clean MISS means nobody holds it; ask the runtime fleet;
 4. stable hash over the live replica set — the degraded path.

Three distinctions in that order are load-bearing, and all three are easy to get
wrong when reimplementing:

* a MISS triggers placement, an OUTAGE must not. Placement needs the same Redis,
  so asking during an outage only adds latency to a request that must degrade
  anyway;
* a MALFORMED record is not a miss. Routing on a guess is worse than degrading:
  the record is the one thing that says who may write, and a corrupt one means
  we do not know;
* a CAPACITY refusal (429 LEASE_UNAVAILABLE) must propagate, never degrade. The
  stable hash would pick one of the instances placement just refused, routing
  around the capacity decision the fleet agreed on.

This module speaks Redis but owns no policy: granting and releasing leases stay
with the runtime, and the placement endpoint stays where it is. It only answers
"where do I send this request".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from datacode_common.lease import (
    decode_lease_record,
    decode_runtime_heartbeat,
    lease_redis_key,
    normalize_runtime_addr,
    runtime_index_redis_key,
    runtime_redis_key,
    stable_hash_pick,
)
from datacode_common.naming import LeaseKeyError, lease_key_from_working_copy_path

logger = logging.getLogger(__name__)

# How stale a routing decision may be.
#
# Short on purpose: a lease can move (drain, crash, rebalance), and a stale
# entry sends work to a replica that no longer owns the copy. Ten seconds is well
# inside the lease TTL, so a moved lease is picked up long before the old
# holder's claim could expire and be regranted. Matches the gateway's CacheTTL —
# the two caches must go stale together, or one of them keeps routing to a
# replica the other has already abandoned.
CACHE_TTL_SECONDS = 10.0

# Bounds one placement round trip. The runtime's accept handshake is sub-second
# (SET NX + flock; materialization continues in the background), so a timeout
# here means the control path is broken, not that a repository is large.
ACQUIRE_TIMEOUT_SECONDS = 5.0


class LeaseUnavailable(RuntimeError):
    """No replica could be resolved for a working copy.

    Distinct from "the request failed": the caller reached nothing, so a retry a
    moment later is expected to succeed. Callers must NOT fall back to the
    load-balanced Service — that would defeat the invariant this resolution
    exists to hold.
    """


class LeaseCapacityRefused(LeaseUnavailable):
    """The fleet has no lease capacity for this working copy right now.

    Typed separately because it must be treated DIFFERENTLY from every other
    resolution failure: a transport hiccup justifies degrading to the stable
    hash, but a capacity verdict is exactly what the stable hash would route
    around.

    Attributes:
        retry_after: The window the fleet advertised, "" when absent.
    """

    def __init__(self, message: str, retry_after: str = "") -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class _CacheEntry:
    addr: str
    expires_at: float


# Injected so a test can drive both the "coordination available" and the
# "coordination down" branches deterministically. Without injection a suite
# running next to a live Redis silently exercises only one of them — the same
# trap the egress grant store documents.
RedisFactory = Callable[[], Awaitable[Optional[Any]]]


async def _shared_redis() -> Optional[Any]:
    """The platform's shared Redis client, or None when unusable.

    None means "coordination unavailable", which every caller reads as "degrade",
    never as "route anywhere". A missing dependency is logged distinctly from an
    unreachable server: an operator seeing "coordination unavailable" while Redis
    is plainly healthy has no way to guess the real cause is an uninstalled
    package.
    """
    try:
        from datacode_common.redis.client import get_redis
    except ImportError as exc:
        logger.error(
            "lease_routing_dependency_missing error=%s hint=%s",
            exc,
            "install the redis extra; lease-aware routing degrades to the "
            "configured base URL until it is present",
        )
        return None
    return get_redis()


class LeasePlacement:
    """Asks the runtime fleet to place a lease nobody holds yet.

    The request may be load balanced to ANY replica: that replica runs placement
    and either serves locally or forwards to the winner (see the runtime's
    src/lease/server.py). This is the one call that is CORRECT to send to the
    Service address, because it is asking "who should own this" rather than
    talking to an owner.
    """

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        *,
        timeout: float = ACQUIRE_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = internal_token
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        """Whether placement can be attempted at all.

        Without a base URL or without the shared secret the endpoint would
        reject us anyway (it is fail-closed), so misses stay on the degraded
        path instead of spending a round trip to learn that.
        """
        return bool(self._base_url and self._token)

    async def acquire(self, lease_key_value: str) -> str:
        """Place a lease and return the dialable address of its new holder.

        Raises:
            LeaseCapacityRefused: 429 — a capacity verdict the caller must not
                route around.
            LeaseUnavailable: placement could not be completed for any other
                reason. Callers degrade on this one.
        """
        import httpx

        from datacode_common.security.internal_token import auth_headers

        if not self.enabled:
            raise LeaseUnavailable("lease placement is not configured")

        url = f"{self._base_url}/internal/leases/acquire"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    json={"lease_key": lease_key_value},
                    headers=auth_headers(self._token),
                )
        except httpx.HTTPError as exc:
            raise LeaseUnavailable(f"lease placement unreachable: {exc}") from exc

        if response.status_code == 429:
            raise LeaseCapacityRefused(
                "no runtime capacity is available for this working copy",
                retry_after=response.headers.get("Retry-After", ""),
            )
        if response.status_code in (401, 403):
            # A deployment error worth its own line: the secret is wrong, and
            # every placement will keep failing until it is fixed.
            logger.error(
                "lease_placement_unauthorized status=%d hint=%s",
                response.status_code,
                "WORKSPACE_RUNTIME_INTERNAL_TOKEN mismatch",
            )
            raise LeaseUnavailable(
                f"lease placement rejected our token ({response.status_code})"
            )
        if response.status_code != 200:
            raise LeaseUnavailable(
                f"lease placement returned status {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise LeaseUnavailable(f"malformed placement response: {exc}") from exc

        addr = normalize_runtime_addr(str(payload.get("addr", "")))
        if not addr:
            # A success without an address is a contract violation on the
            # runtime side; routing on it would send traffic nowhere.
            raise LeaseUnavailable(
                f"placement response for {lease_key_value!r} carries no addr"
            )
        logger.info(
            "lease_placed_for_routing lease_key=%s runtime_id=%s",
            lease_key_value,
            payload.get("runtime_id", ""),
        )
        return addr


@dataclass
class LeaseRouter:
    """Resolves working copies to the runtime replica that owns them.

    Attributes:
        fallback_url: The load-balanced Service address. Used when leases are
            DISABLED entirely (single-replica deployments, dev) and for requests
            that carry no working copy — many routes are correctly served by any
            replica, so an unparseable path is not an error.
        placement: Optional placement client. Absent means a lease miss goes
            straight to the degraded path.
        redis_factory: Injectable coordination client accessor.
        cache_ttl: Lifetime of a cached decision.
    """

    fallback_url: str = ""
    placement: Optional[LeasePlacement] = None
    redis_factory: RedisFactory = _shared_redis
    cache_ttl: float = CACHE_TTL_SECONDS
    # No lock guards this: an asyncio service mutates it only between awaits, and
    # a duplicated lookup under a race is harmless (both resolve the same holder)
    # whereas a lock here would serialize every request behind one dict write.
    _cache: dict[str, _CacheEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.fallback_url = (self.fallback_url or "").rstrip("/")

    async def resolve(self, workspace_path: str) -> str:
        """Return the base URL to send this working copy's requests to.

        A path that is not a working copy is NOT an error: metadata routes carry
        none, and those are correctly served by any replica.

        Raises:
            LeaseCapacityRefused: the fleet refused to place a lease.
            LeaseUnavailable: nothing could be resolved and no fallback exists.
        """
        try:
            key = lease_key_from_working_copy_path(workspace_path)
        except LeaseKeyError:
            return self.fallback_url

        return await self.resolve_lease_key(key)

    async def resolve_lease_key(self, key: str, *, allow_placement: bool = True) -> str:
        """Resolve a lease key that the caller already holds.

        The control plane addresses working copies by logical identifiers
        (tenant / workspace / user / environment) and has no filesystem to
        derive a path from, so it builds the key itself. Everything after the
        derivation is shared with :meth:`resolve` on purpose: two resolution
        policies would eventually disagree about who may write.

        ``allow_placement=False`` resolves without ever asking the fleet to
        place a lease. Placement makes the winner CLONE the repository, which
        is right before writing to a copy and wrong for reading about one: a
        probe would otherwise materialize files in order to report that they
        were not there, and the answer it returns would be a fact it created
        itself.

        Raises:
            LeaseCapacityRefused: the fleet refused to place a lease.
            LeaseUnavailable: nothing could be resolved and no fallback exists.
        """
        cached = self._cached(key)
        if cached:
            return cached

        client = await self.redis_factory()
        if client is None:
            # Leases may simply be off (single replica, shared medium). That is
            # a legitimate deployment, and the Service address is the right
            # answer for it.
            return self._require_fallback(key)

        addr, outage = await self._lookup(client, key)
        if addr:
            self._store(key, addr)
            return addr

        if (
            allow_placement
            and not outage
            and self.placement is not None
            and self.placement.enabled
        ):
            # Clean miss: nobody holds this copy yet.
            try:
                placed = await self.placement.acquire(key)
            except LeaseCapacityRefused:
                # Final for this request — degrading here would route around the
                # capacity decision placement just made.
                raise
            except LeaseUnavailable as exc:
                logger.warning("lease_placement_failed lease_key=%s error=%s", key, exc)
            else:
                self._store(key, placed)
                return placed

        degraded = await self._stable_hash(client, key)
        if degraded:
            logger.warning(
                "lease_routing_degraded lease_key=%s mode=stable_hash risk=%s",
                key,
                "replica-count changes may pick a second writer; the runtime "
                "flock is the remaining guard",
            )
            return degraded

        return self._require_fallback(key)

    def invalidate(self, workspace_path: str) -> None:
        """Drop a cached decision.

        Called when a request FAILS or is answered with ``LEASE_NOT_HELD``: both
        mean the cached route is wrong, and continuing to use it for the rest of
        the TTL would extend one hand-over into a multi-second outage for that
        user.
        """
        try:
            key = lease_key_from_working_copy_path(workspace_path)
        except LeaseKeyError:
            return
        self._cache.pop(key, None)

    def invalidate_lease_key(self, key: str) -> None:
        """Drop a cached decision addressed by lease key (see :meth:`invalidate`)."""
        self._cache.pop(key, None)

    # ── internals ──────────────────────────────────────────────────────

    async def _lookup(self, client: Any, key: str) -> tuple[str, bool]:
        """Read the authoritative record.

        Returns ``(addr, outage)``. ``("", False)`` is a clean miss — nobody
        holds the lease — and ``("", True)`` means the backend could not answer.
        Callers must keep these apart: a miss should trigger placement, an
        outage must not.

        A malformed record also yields a miss-with-outage-semantics (no
        placement, degrade instead): the record is the one thing that says who
        may write, so a corrupt one means we do not know, and asking for a new
        placement while an unreadable claim exists could mount the copy twice.
        """
        try:
            raw = await client.get(lease_redis_key(key))
        except Exception as exc:  # noqa: BLE001 — connection errors take many shapes
            logger.warning("lease_lookup_failed lease_key=%s error=%s", key, exc)
            return "", True

        if raw is None:
            return "", False

        record = decode_lease_record(key, _text(raw))
        if record is None or not record.runtime_addr:
            logger.warning(
                "lease_record_unusable lease_key=%s hint=%s",
                key,
                "degrading rather than routing on a guess",
            )
            return "", True
        return normalize_runtime_addr(record.runtime_addr), False

    async def _stable_hash(self, client: Any, key: str) -> str:
        """Pick a replica deterministically from the live runtime set."""
        try:
            members = await client.smembers(runtime_index_redis_key())
        except Exception as exc:  # noqa: BLE001
            logger.warning("lease_replica_set_unreadable error=%s", exc)
            return ""

        addrs: list[str] = []
        for member in members or ():
            runtime_id = _text(member)
            if not runtime_id:
                continue
            try:
                raw = await client.get(runtime_redis_key(runtime_id))
            except Exception:  # noqa: BLE001
                continue
            if raw is None:
                # Expired heartbeat: the index is a SET and cannot expire
                # per-member, so a stale id is normal and simply skipped.
                continue
            heartbeat = decode_runtime_heartbeat(_text(raw))
            if heartbeat is None or not heartbeat.addr or heartbeat.draining:
                # A draining replica keeps its existing leases but must not be
                # handed new work by the fallback path: it is about to go away.
                continue
            addrs.append(normalize_runtime_addr(heartbeat.addr))

        return stable_hash_pick(key, addrs)

    def _require_fallback(self, key: str) -> str:
        if self.fallback_url:
            return self.fallback_url
        raise LeaseUnavailable(f"no runtime available for working copy {key!r}")

    def _cached(self, key: str) -> str:
        entry = self._cache.get(key)
        if entry is None or time.monotonic() >= entry.expires_at:
            return ""
        return entry.addr

    def _store(self, key: str, addr: str) -> None:
        self._cache[key] = _CacheEntry(
            addr=addr, expires_at=time.monotonic() + self.cache_ttl
        )


def _text(value: Any) -> str:
    """Decode a Redis value regardless of the client's decode_responses setting."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "ACQUIRE_TIMEOUT_SECONDS",
    "CACHE_TTL_SECONDS",
    "LeaseCapacityRefused",
    "LeasePlacement",
    "LeaseRouter",
    "LeaseUnavailable",
]
