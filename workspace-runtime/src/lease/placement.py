"""Lease placement — choosing which instance serves a new working copy.

Placement is a STATELESS decision over shared data (design 03 §3.3): any
process reading the same Redis — a runtime instance, or control-service — can
execute it, because the inputs (the runtime index and the heartbeats) live
there and the output is just a choice. That is why this module takes a client
and returns a decision rather than owning a scheduler.

Why the runtime is still the enforcement point
----------------------------------------------
This decision reads heartbeats that may be a whole interval (10s) stale, so two
concurrent placements can pick the same "emptiest" instance. The chosen instance
re-checks its own limits in :meth:`LeaseManager.serve` before accepting
(design 07 Q3): the count limit and the memory watermark are enforced against
live state, locally. Placement here is best-effort spreading; admission is a
promise.

What "emptiest" means
---------------------
Ordered by ``(tenant_pressure, lease_count, memory_used_ratio)``:

* ``lease_count`` spreads workspaces evenly by number, and the ratio breaks
  ties in favour of the instance with the most headroom — which matters
  because the real cost of a lease scales with the USER'S repository size, not
  with the count (a 2000-model repo and a 50-model one differ by an order of
  magnitude). Instances over the memory watermark are skipped entirely rather
  than merely deprioritized: sorting them lower would still place on them when
  they alone remain.
* ``tenant_pressure`` (design 03 §6 — weighted fairness) comes FIRST. It is the
  requesting tenant's occupancy of that instance relative to its
  ``tenant_lease_share`` weight, and its job is to stop one tenant's batch
  operation from claiming every instance's headroom at once: without it, a
  tenant acquiring sixty leases back-to-back wins every "emptiest instance"
  race during its burst, and a small tenant's single acquire lands on whatever
  is left — typically the most loaded box. With the pressure term, the big
  tenant's burst spreads itself out first, and the small tenant's request
  ranks every instance it has not touched as strictly preferable.

  A weight of 0 (or no quota row published) means every tenant weighs the
  same, which reduces the term to plain per-tenant spreading — the behaviour
  before fairness existed, so enabling the feature can never make placement
  WORSE than the spread-only baseline.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from datacode_common.lease import (
    RuntimeHeartbeat,
    decode_runtime_heartbeat,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Placement:
    """The outcome of one placement decision."""

    runtime_id: str
    addr: str
    # The heartbeat the decision was based on, for the log line: when placement
    # was wrong, this is how one tells "stale data" from "bad policy".
    heartbeat: RuntimeHeartbeat


class NoInstanceAvailable(RuntimeError):
    """No instance can take another lease right now."""


async def pick_instance(
    client,
    *,
    tenant_id: str = "",
    exclude: t.Container[str] = (),
    memory_high_watermark: float = 0.80,
) -> Placement:
    """Choose the instance that should serve the next lease.

    Args:
        tenant_id: The requesting tenant, parsed from the lease key by the
            caller. Drives the fairness term: an instance this tenant already
            occupies heavily ranks lower than one it has not touched, unless
            the spreading term says otherwise (see the module docstring).
        exclude: Lets a caller retry: if the first choice refused (lease taken
            elsewhere, flock contention), the next call skips it rather than
            picking it again deterministically — which would turn a retry loop
            into a hammer on one unlucky instance.

    Async because the platform's shared Redis client is async and loop-bound:
    placement runs on the request path and shares that loop.
    """
    members = await client.smembers(runtime_index_redis_key())
    if not members:
        raise NoInstanceAvailable("runtime index is empty")

    candidates: list[RuntimeHeartbeat] = []
    for runtime_id in members:
        if runtime_id in exclude:
            continue
        raw = await client.get(runtime_redis_key(runtime_id))
        if not raw:
            # Expired heartbeat: the index is a set and cannot expire
            # per-member, so a crashed instance's id lingers. Skipping is the
            # only correct treatment — routing a lease to a dead pod turns
            # every subsequent request into a connection error.
            continue
        if isinstance(raw, bytes):
            raw = raw.decode()
        heartbeat = decode_runtime_heartbeat(raw)
        if heartbeat is None:
            logger.warning("placement_heartbeat_undecodable")
            continue
        if not heartbeat.has_capacity(memory_high_watermark=memory_high_watermark):
            continue
        candidates.append(heartbeat)

    if not candidates:
        raise NoInstanceAvailable(
            f"no instance with capacity among {len(members)} indexed"
        )

    # The fairness weight is resolved BEFORE sorting: the sort key itself is
    # synchronous, and the weight lives behind an async Redis read. It shares
    # the weight with the sort by closure rather than parameter.
    share = await _tenant_lease_share(client, tenant_id)

    def sort_key(hb: RuntimeHeartbeat) -> tuple[float, int, float]:
        tenant_pressure = (
            _tenant_occupancy(hb, tenant_id) / share if share > 0 else 0.0
        )
        return (tenant_pressure, hb.lease_count, hb.memory_used_ratio)

    chosen = min(candidates, key=sort_key)
    logger.info(
        "placement_decision",
        runtime_id=chosen.runtime_id,
        tenant_id=tenant_id or "unknown",
        tenant_lease_share=share,
        lease_count=chosen.lease_count,
        memory_used_ratio=round(chosen.memory_used_ratio, 3),
        candidates=len(candidates),
    )
    return Placement(
        runtime_id=chosen.runtime_id, addr=chosen.addr, heartbeat=chosen
    )


def _tenant_occupancy(heartbeat: RuntimeHeartbeat, tenant_id: str) -> int:
    """How many of the instance's leases belong to ``tenant_id``.

    Zero when the tenant is unknown: an unattributable placement falls back to
    pure spreading, which is the safe direction (fairness only ever reorders
    candidates, it never rejects them).
    """
    if not tenant_id:
        return 0
    return sum(
        1 for lease_key in heartbeat.leases if lease_key.split("/", 1)[0] == tenant_id
    )


async def _tenant_lease_share(client, tenant_id: str) -> int:
    """The tenant's fairness weight, from the quota snapshot in Redis.

    Reads the per-tenant override first, then the platform defaults — the same
    resolution order the lease-quota guard uses. Absent, unreadable or zero
    means "every tenant weighs the same", which the sort key turns into "no
    fairness term": the pre-fairness spreading behaviour, so enabling the
    feature can never make placement worse than that baseline.
    """
    if not tenant_id:
        return 0
    from datacode_common.lease import (
        decode_quota_snapshot,
        quota_defaults_redis_key,
        tenant_quota_redis_key,
    )

    for key in (tenant_quota_redis_key(tenant_id), quota_defaults_redis_key()):
        try:
            raw = await client.get(key)
        except Exception:  # noqa: BLE001 — fairness is an optimization
            return 0
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode()
        snapshot = decode_quota_snapshot(raw)
        if snapshot is not None and snapshot.tenant_lease_share:
            return int(snapshot.tenant_lease_share)
    return 0
