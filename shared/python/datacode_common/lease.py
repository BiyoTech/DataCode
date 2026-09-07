"""Workspace lease state contract (design 03 §3.3, design 05 §5.2, P4a).

The lease is what makes every other P4 simplification legal: because exactly
one runtime instance owns a working copy at a time, the platform may keep the
working copy on replica-private storage, degrade environment locks to
in-process ones, and cache SQLMesh contexts without cross-replica
invalidation. This module owns the *shared vocabulary* of that mechanism —
the Redis key layout, the record shapes and the TTL semantics — so the three
parties (control-service grants, workspace-runtime holds, edge-gateway routes)
cannot drift apart.

It deliberately contains no I/O. Grant/renew/release policy lives in
control-service and the holder side in workspace-runtime; putting the shapes
here keeps a Redis key from being spelled two different ways in two languages.

TTL is a CRASH-RECOVERY BOUND, not an operation timeout
------------------------------------------------------
A holder renews its lease every ``ttl/3`` while alive, so a live lease never
expires mid-operation however long an ``apply`` runs. The TTL only decides how
long a *dead* holder's working copy stays unreachable before another instance
may take it — which is why it is measured in seconds (120) rather than in the
duration of the longest operation. Making it an operation timeout would either
strand long applies or leave crashed holders' workspaces stuck for hours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence

from datacode_common.naming import split_lease_key

__all__ = [
    "LEASE_TTL_SECONDS",
    "LEASE_RENEW_DIVISOR",
    "CAS_DELETE_SCRIPT",
    "CAS_RENEW_SCRIPT",
    "LeaseState",
    "Lease",
    "LeaseRecord",
    "RuntimeHeartbeat",
    "decode_lease_record",
    "decode_runtime_heartbeat",
    "encode_runtime_heartbeat",
    "normalize_runtime_addr",
    "stable_hash_index",
    "stable_hash_pick",
    "TenantQuotaSnapshot",
    "decode_quota_snapshot",
    "encode_quota_snapshot",
    "lease_redis_key",
    "lease_key_of_redis_key",
    "lease_redis_key_pattern",
    "runtime_redis_key",
    "runtime_index_redis_key",
    "tenant_quota_redis_key",
    "quota_defaults_redis_key",
    "renew_interval_seconds",
]

# Default crash-recovery bound. Overridable per deployment via
# `lease.ttl_seconds`; the divisor below is NOT configurable because a holder
# that renews less than twice per TTL would lose its lease during a single
# lost packet.
LEASE_TTL_SECONDS = 120

# Renew every ttl/3: two consecutive renewals may fail before the lease is
# lost, which tolerates a transient Redis blip without widening the window in
# which a crashed holder blocks its workspace.
LEASE_RENEW_DIVISOR = 3


def renew_interval_seconds(ttl_seconds: int = LEASE_TTL_SECONDS) -> float:
    """Watchdog renewal period for a given TTL."""
    if ttl_seconds <= 0:
        raise ValueError(f"lease ttl must be positive, got {ttl_seconds}")
    return ttl_seconds / LEASE_RENEW_DIVISOR


class LeaseState(str, Enum):
    """Lifecycle of a lease, as seen by the routing layer.

    ``DRAINING`` exists so a graceful shutdown is distinguishable from a
    crash: the gateway must keep sending requests for in-flight operations to
    a draining holder (it still owns the working copy) while the controller
    stops granting it new leases. Collapsing it into "gone" would abort
    running applies; collapsing it into "held" would keep routing new work to
    a shutting-down pod.
    """

    HELD = "held"
    DRAINING = "draining"
    RELEASED = "released"


@dataclass(frozen=True)
class Lease:
    """One working copy's ownership record.

    Stored in Redis under :func:`lease_redis_key` with the TTL as the physical
    expiry, so a crashed holder's record disappears without anyone running a
    reaper. ``expires_at`` is carried redundantly for observability (the
    reconciler and /health report it); Redis remains the authority.
    """

    lease_key: str
    runtime_id: str
    # Address the gateway dials directly, bypassing the Service: routing to a
    # specific pod is the entire point of the lease.
    runtime_addr: str
    state: LeaseState
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        # Validate the key here rather than trusting callers: a malformed key
        # would namespace a Redis entry that no one can find again.
        split_lease_key(self.lease_key)
        for name in ("acquired_at", "expires_at"):
            value: datetime = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(
                    f"Lease.{name} must be timezone-aware UTC, got naive {value!r}"
                )

    @property
    def is_routable(self) -> bool:
        """Whether the gateway may forward requests to this holder.

        Draining holders stay routable on purpose — see :class:`LeaseState`.
        """
        return self.state in (LeaseState.HELD, LeaseState.DRAINING)

    def seconds_until_expiry(self, *, now: datetime | None = None) -> float:
        """Remaining lifetime; negative once the record is stale."""
        reference = now or datetime.now(timezone.utc)
        return (self.expires_at - reference).total_seconds()

    def renewed(
        self, *, ttl_seconds: int = LEASE_TTL_SECONDS, now: datetime | None = None
    ) -> "Lease":
        """Return this lease with its expiry pushed out by ``ttl_seconds``."""
        reference = now or datetime.now(timezone.utc)
        return replace(self, expires_at=reference + timedelta(seconds=ttl_seconds))


@dataclass(frozen=True)
class LeaseRecord:
    """The RAW Redis payload of one lease, as the holder wrote it.

    :class:`Lease` is the routing view (typed timestamps, state); this is the
    wire view a THIRD PARTY sees when it reads the key without holding it — the
    reconciler's fifth party (design 05 §5.1). It exists because that reader
    needs the ownership ``token``, which the routing view deliberately omits:
    deleting a stale lease is a compare-and-swap on that token, and a reader
    that could not see it would have to blind-``DEL`` someone else's claim.

    Unknown fields are ignored on decode, and ``acquired_at`` is the epoch
    seconds the holder recorded — so it is UTC by construction, whatever the
    reader's local zone.
    """

    lease_key: str
    token: str
    runtime_id: str
    runtime_addr: str
    acquired_at: datetime

    def __post_init__(self) -> None:
        split_lease_key(self.lease_key)
        if self.acquired_at.tzinfo is None:
            raise ValueError("LeaseRecord.acquired_at must be timezone-aware UTC")

    def age_seconds(self, *, now: datetime | None = None) -> float:
        """How long this lease has existed; the staleness input."""
        reference = now or datetime.now(timezone.utc)
        return (reference - self.acquired_at).total_seconds()


def decode_lease_record(lease_key: str, raw: str) -> "LeaseRecord | None":
    """Decode the payload stored under :func:`lease_redis_key`.

    ``None`` when the payload cannot be understood. Deliberately parallel to
    :func:`decode_runtime_heartbeat`: an undecodable record is treated as
    ABSENT rather than guessed at, because every caller's next move (delete it,
    report it as drift) would otherwise act on invented ownership.
    """
    try:
        payload = json.loads(raw)
        return LeaseRecord(
            lease_key=lease_key,
            token=str(payload["token"]),
            runtime_id=str(payload["runtime_id"]),
            runtime_addr=str(payload.get("runtime_addr", "")),
            acquired_at=datetime.fromtimestamp(
                float(payload["acquired_at"]), tz=timezone.utc
            ),
        )
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        return None


# ── Routing primitives (shared by every lease-aware consumer) ─────────────
# These two functions turn a lease record into something dialable. They live in
# the contract module rather than next to a router because there is now more
# than one router: the edge gateway (Go) and agent-service (Python) must reach
# the SAME replica for the same working copy, and two independent
# implementations of "which one" is precisely how a second writer appears.


def normalize_runtime_addr(addr: str) -> str:
    """Turn a stored lease address into a dialable base URL.

    The holder stores ``host:port`` because it does not know how a caller
    reaches it — the scheme is the caller's concern. Defaulting to ``http`` is
    correct for in-cluster pod-to-pod traffic, and an address that already
    carries a scheme is passed through so a TLS deployment needs no code change.

    Mirrors ``lease.NormalizeAddr`` in shared/go/lease/state.go.
    """
    if not addr:
        return ""
    if addr.startswith(("http://", "https://")):
        return addr.rstrip("/")
    return "http://" + addr.rstrip("/")


# FNV-1a (32-bit) parameters. The algorithm is pinned, not chosen per language:
# both routers must map a lease key to the SAME index of the same sorted replica
# list, or the degraded path would put two writers on one working copy — the
# exact failure it exists to avoid.
_FNV_OFFSET_BASIS_32 = 0x811C9DC5
_FNV_PRIME_32 = 0x01000193
_UINT32_MASK = 0xFFFFFFFF


def stable_hash_index(lease_key_value: str, count: int) -> int:
    """Index of the replica a lease key maps to, over ``count`` candidates.

    Used ONLY on the degraded path, when the authoritative lease record cannot
    be read. It is weaker than a lease and deliberately so (design 05 §5.2):
    while the replica count is stable it sends every request for one working
    copy to one replica, preserving the invariant; if the count changes during
    the outage two replicas can be chosen and the runtime's flock is the last
    line of defence. Failing every request instead would turn a degraded
    coordination backend into a total outage.

    Mirrors the FNV-1a computation in the gateway's ``Router.stableHash``.

    Raises:
        ValueError: ``count`` is not positive — there is no valid index to
            return, and a caller that ignored a sentinel would dial index 0 of
            an empty list.
    """
    if count <= 0:
        raise ValueError(f"stable hash needs at least one candidate, got {count}")
    digest = _FNV_OFFSET_BASIS_32
    for byte in lease_key_value.encode("utf-8"):
        digest = ((digest ^ byte) * _FNV_PRIME_32) & _UINT32_MASK
    return digest % count


def stable_hash_pick(lease_key_value: str, candidates: Sequence[str]) -> str:
    """Pick one candidate deterministically, sorting first.

    The sort is not cosmetic: replica sets arrive from an unordered Redis SET,
    and two routers reading them in different orders would disagree about who
    owns a working copy while each believed itself stable.
    """
    ordered = sorted(c for c in candidates if c)
    if not ordered:
        return ""
    return ordered[stable_hash_index(lease_key_value, len(ordered))]


# ── Ownership compare-and-swap (shared by every lease mutator) ─────────────
# A lease's Redis value is a JSON payload, and its ownership proof is ONE field
# of it (``token``). Every mutation must therefore compare that field — not the
# whole value, which also carries ``acquired_at`` and would never match a token,
# and not a substring, which would let a token appearing inside another field
# authorize the write.
#
# These live here rather than next to their callers because there are now three:
# the holder's release, the holder's watchdog renew, and the reconciler's
# stale-lease repair. Three copies of a compare-and-swap is three chances for one
# of them to become a blind DEL.
#
# ``pcall`` keeps a corrupt payload from erroring the script: it simply does not
# match, which is the same verdict :func:`decode_lease_record` reaches.
_CAS_PREAMBLE = """
local current = redis.call('GET', KEYS[1])
if current == false then
  return 0
end
local ok, record = pcall(cjson.decode, current)
if not ok or type(record) ~= 'table' or record['token'] ~= ARGV[1] then
  return 0
end
"""

# Delete the lease only while we still own it.
CAS_DELETE_SCRIPT = _CAS_PREAMBLE + "return redis.call('DEL', KEYS[1])\n"

# Extend the lease only while we still own it. ARGV[2] is the new TTL in ms.
CAS_RENEW_SCRIPT = _CAS_PREAMBLE + "return redis.call('PEXPIRE', KEYS[1], ARGV[2])\n"


@dataclass(frozen=True)
class RuntimeHeartbeat:
    """A runtime instance's self-report, used to place new leases.

    Capacity is reported by the instance rather than computed centrally
    because the real constraint is that instance's memory budget against the
    size of the repositories it happens to hold (design 07 Q3): a controller
    counting leases would place a 2000-model repo as if it cost the same as a
    50-model one.
    """

    runtime_id: str
    addr: str
    lease_count: int
    max_leases: int
    # Fraction of the pod's memory budget in use. The admission decision needs
    # measured pressure, not just the count, so an underestimated per-lease
    # cost cannot lead to an OOM.
    memory_used_ratio: float
    reported_at: datetime
    draining: bool = False
    leases: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.reported_at.tzinfo is None:
            raise ValueError("RuntimeHeartbeat.reported_at must be timezone-aware UTC")

    def has_capacity(self, *, memory_high_watermark: float = 0.80) -> bool:
        """Whether this instance may accept one more lease.

        Both limits must hold: the count keeps a single instance from holding
        an unbounded number of small workspaces, and the memory watermark is
        what actually prevents an OOM when the estimate is wrong.
        """
        if self.draining:
            return False
        if self.max_leases > 0 and self.lease_count >= self.max_leases:
            return False
        return self.memory_used_ratio < memory_high_watermark


# ── Heartbeat wire format ─────────────────────────────────────────────────
# The JSON shape below is the CONTRACT between the writer (workspace-runtime's
# HeartbeatReporter) and two independent readers: runtime placement (request
# path) and control-service's LeaseController (capacity loop). It lives here —
# next to the dataclass it produces — so the encoder cannot drift from the
# decoder the way two copy-pasted readers eventually would. Unknown fields are
# ignored on decode: a newer writer may publish extra keys, and an older reader
# that rejected them would blank a healthy instance out of placement.


def encode_runtime_heartbeat(heartbeat: RuntimeHeartbeat) -> str:
    """Serialize a heartbeat to the JSON wire format stored in Redis."""
    return json.dumps(
        {
            "runtime_id": heartbeat.runtime_id,
            "addr": heartbeat.addr,
            "lease_count": heartbeat.lease_count,
            "max_leases": heartbeat.max_leases,
            "memory_used_ratio": heartbeat.memory_used_ratio,
            # UTC ISO-8601: the only format that survives crossing a
            # process/language boundary unambiguously.
            "reported_at": heartbeat.reported_at.isoformat(),
            "draining": heartbeat.draining,
            "leases": list(heartbeat.leases),
        }
    )


def decode_runtime_heartbeat(raw: str) -> RuntimeHeartbeat | None:
    """Decode one heartbeat, tolerating a shape this version does not know.

    An undecodable heartbeat is treated as ABSENT, not as an empty instance:
    the writer is either a newer version (whose instances we cannot safely
    reason about) or something is wrong, and either way "skip it" is the answer
    that cannot misplace a lease or miscount capacity.
    """
    try:
        payload = json.loads(raw)
        return RuntimeHeartbeat(
            runtime_id=payload["runtime_id"],
            addr=payload["addr"],
            lease_count=int(payload["lease_count"]),
            max_leases=int(payload["max_leases"]),
            memory_used_ratio=float(payload["memory_used_ratio"]),
            reported_at=datetime.fromisoformat(payload["reported_at"]),
            draining=bool(payload.get("draining", False)),
            leases=tuple(payload.get("leases", ())),
        )
    except (ValueError, KeyError, TypeError):
        return None


# ── Tenant lease quota snapshot (P8) ──────────────────────────────────────
# control-service is the quota authority (it owns tenant_quotas) but the
# lease-acquire path runs on workspace-runtime and must not call a database on
# it. The control plane therefore PUBLISHES the lease-relevant limits to Redis
# and the runtime reads them back — same pattern, same reasons, as the
# heartbeats flowing the other way. ``None`` means "no limit" (unlimited), and
# an absent key means "nothing published", which the runtime treats as
# enforcement-off rather than zero: quota is capacity governance, and losing
# the control plane must not lock every tenant out of its working copies.


@dataclass(frozen=True)
class TenantQuotaSnapshot:
    """The lease-relevant slice of one tenant's quota (or the defaults)."""

    # ``None`` = unlimited; 0 = an explicit ban.
    max_concurrent_leases: int | None
    max_runtime_replicas: int | None
    # When the authority published this snapshot (UTC).
    updated_at: datetime
    # Fairness weight (design 03 §6): how many of the tenant's leases one
    # instance absorbs before placement starts preferring instances the tenant
    # has not touched. ``None``/0 = every tenant weighs the same (spread-only,
    # the pre-fairness behaviour). Carries a default so existing constructors
    # (which pass the fields above by keyword) need no change.
    tenant_lease_share: int | None = None

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            raise ValueError("TenantQuotaSnapshot.updated_at must be timezone-aware UTC")


def encode_quota_snapshot(snapshot: TenantQuotaSnapshot) -> str:
    """Serialize a quota snapshot to the JSON wire format stored in Redis."""
    return json.dumps(
        {
            "max_concurrent_leases": snapshot.max_concurrent_leases,
            "max_runtime_replicas": snapshot.max_runtime_replicas,
            # Always written (null when unset) so the key's shape is stable;
            # an older reader simply ignores the field it does not know.
            "tenant_lease_share": snapshot.tenant_lease_share,
            "updated_at": snapshot.updated_at.isoformat(),
        }
    )


def decode_quota_snapshot(raw: str) -> TenantQuotaSnapshot | None:
    """Decode a quota snapshot; ``None`` when malformed (enforcement-off).

    Deliberately parallel to :func:`decode_runtime_heartbeat`: a malformed
    record is skipped rather than guessed at, and the caller's documented
    failure mode is "no quota applied", which degrades safely. The fairness
    weight is optional: a snapshot written before the field existed decodes
    with ``None``, which placement reads as "spread-only".
    """
    try:
        payload = json.loads(raw)
        return TenantQuotaSnapshot(
            max_concurrent_leases=_optional_int(payload.get("max_concurrent_leases")),
            max_runtime_replicas=_optional_int(payload.get("max_runtime_replicas")),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            tenant_lease_share=_optional_int(payload.get("tenant_lease_share")),
        )
    except (ValueError, KeyError, TypeError):
        return None


def _optional_int(value) -> int | None:
    """Coerce a wire value to int, passing ``None`` through as unlimited."""
    if value is None:
        return None
    return int(value)


# ── Redis key layout ──────────────────────────────────────────────────────
# All keys go through datacode_common.redis.redis_key, which already prefixes
# {key_prefix}:{cell_id} — so a lease in cell-0 can never be mistaken for one
# in cell-1 even when both share a Redis instance (design 03 §2).


def lease_redis_key(lease_key: str) -> str:
    """Redis key holding the :class:`Lease` for one working copy."""
    split_lease_key(lease_key)
    from datacode_common.redis import redis_key

    return redis_key("lease", lease_key)


def lease_redis_key_pattern() -> str:
    """SCAN pattern matching every lease key of THIS cell.

    Used by the reconciler's fifth party, which must enumerate leases rather
    than look one up. Deriving the pattern from the same builder as the keys
    themselves is what keeps the cell prefix in it: a hand-written
    ``"lease:*"`` would scan another cell's leases and report every one of them
    as an orphan.
    """
    from datacode_common.redis import redis_key

    return f"{redis_key('lease', '')}*"


def lease_key_of_redis_key(redis_key_value: str) -> str:
    """Recover the lease key from a full Redis key, or "" when it is not one.

    The inverse of :func:`lease_redis_key`, needed because SCAN returns full
    keys while every consumer (audit, split, tenant attribution) speaks lease
    keys. Returns "" rather than raising: SCAN may surface a key written by a
    different version, and one unparseable entry must not abort a whole pass.
    """
    prefix = lease_redis_key_pattern()[:-1]  # drop the trailing "*"
    if not redis_key_value.startswith(prefix):
        return ""
    candidate = redis_key_value[len(prefix) :]
    try:
        split_lease_key(candidate)
    except ValueError:
        return ""
    return candidate


def runtime_redis_key(runtime_id: str) -> str:
    """Redis key holding one instance's :class:`RuntimeHeartbeat`."""
    if not runtime_id:
        raise ValueError("runtime_id must not be empty")
    from datacode_common.redis import redis_key

    return redis_key("rt", runtime_id)


def runtime_index_redis_key() -> str:
    """Redis key of the set of live runtime ids.

    An explicit index instead of ``SCAN rt:*``: placement runs on the request
    path when a lease is missed, and SCAN's cost grows with the whole keyspace
    rather than with the number of runtimes.
    """
    from datacode_common.redis import redis_key

    return redis_key("rt", "index")


def tenant_quota_redis_key(tenant_id: str) -> str:
    """Redis key holding one tenant's lease-quota overrides (P8).

    Written by control-service (the quota authority), read by the runtime's
    acquire path. Carries a TTL so a dead control plane degrades to
    enforcement-off instead of freezing quotas forever.
    """
    if not tenant_id:
        raise ValueError("tenant_id must not be empty")
    from datacode_common.redis import redis_key

    return redis_key("quota", tenant_id)


def quota_defaults_redis_key() -> str:
    """Redis key holding the platform-wide default lease quotas (P8).

    Applied to tenants that have no ``tenant_quotas`` row — the enforcement
    layer resolves defaults, not the schema.
    """
    from datacode_common.redis import redis_key

    return redis_key("quota", "defaults")
