"""Tenant capacity guard for the lease-acquire path (design 06).

The acquire path may not touch a database (see the quota publisher's docstring
on the control side), so the tenant's lease footprint is measured from the
heartbeat stream — the same stream placement already decides on. Two limits
are enforced:

``max_concurrent_leases``
    The tenant's total held working copies across every instance. Exceeding it
    is a hard 429: the limit exists to bound the tenant's aggregate memory
    footprint, and admitting "just one more" is exactly the outcome it forbids.

``max_runtime_replicas``
    The number of DISTINCT instances the tenant occupies. Enforced by
    steering: at the cap, placement candidates are restricted to the instances
    the tenant already occupies, so the lease lands on the existing footprint
    instead of spreading it. When even those cannot take it (full, draining,
    gone), the acquire fails as a quota rejection — the tenant's footprint is
    full, which is the honest answer.

Semantics worth pinning
-----------------------
* **Re-acquisition is not growth.** Acquiring a lease key that a live instance
  already reports held replaces that lease; it must not count as +1 or the
  count would drift upward on every Redis-record expiry race.
* **Fail-open on missing data.** No quota published (control plane down or
  enforcement off) or no heartbeats readable → the guard is a no-op. A Redis
  blip must not turn into "no tenant can open a workspace".
* **Heartbeat staleness is bounded** by the reporter's TTL (a few intervals);
  the admission re-check on the winning instance remains the real protection
  against a stale count, exactly as for placement.
"""

from __future__ import annotations

import typing as t

from datacode_common.lease import (
    RuntimeHeartbeat,
    TenantQuotaSnapshot,
    decode_quota_snapshot,
    decode_runtime_heartbeat,
    quota_defaults_redis_key,
    runtime_index_redis_key,
    runtime_redis_key,
    tenant_quota_redis_key,
)
from datacode_common.naming import split_lease_key

from src.config import settings
from src.utils.errors import QuotaExceededError
from src.utils.logging import get_logger

logger = get_logger(__name__)


class TenantCapacityGuard:
    """One acquire's pre-placement capacity decision, loaded once per call."""

    def __init__(
        self,
        *,
        tenant_id: str,
        lease_key: str,
        quota: TenantQuotaSnapshot,
        heartbeats: t.Sequence[RuntimeHeartbeat],
    ) -> None:
        self._tenant_id = tenant_id
        self._lease_key = lease_key
        self._quota = quota
        self._heartbeats = heartbeats

    @classmethod
    async def load(cls, client, lease_key: str) -> t.Optional["TenantCapacityGuard"]:
        """Load the tenant's quota and the live heartbeats, or ``None``.

        ``None`` means enforcement is off for this acquire: nothing published,
        or the heartbeat stream could not be read. Both degrade to "place as
        before", never to "reject everything".
        """
        tenant_id = split_lease_key(lease_key)[0]

        quota = await cls._read_quota(client, tenant_id)
        if quota is None:
            return None

        heartbeats = await _live_heartbeats(client)
        if heartbeats is None:
            return None

        return cls(
            tenant_id=tenant_id,
            lease_key=lease_key,
            quota=quota,
            heartbeats=heartbeats,
        )

    # ── decisions ─────────────────────────────────────────────────────

    def assert_within_lease_budget(self) -> None:
        """429 when this acquire would push the tenant past its lease ceiling."""
        limit = self._quota.max_concurrent_leases
        if limit is None:
            return

        held = self._held_lease_count()
        # A lease some live instance still reports is REPLACED by this
        # acquire, not added to — see the module docstring.
        already_held = self._is_already_held()
        effective = held if already_held else held + 1

        if effective > limit:
            raise QuotaExceededError(
                "concurrent lease",
                tenant_id=self._tenant_id,
                limit=limit,
                current=held,
                retry_after_seconds=settings.lease_quota_retry_after_seconds,
            )

    def placement_exclusions(self) -> set[str]:
        """Instances placement must skip for this acquire (replica steering).

        Empty while the tenant is under its replica cap. At the cap, every
        instance the tenant does NOT already occupy is excluded, so the lease
        consolidates onto the existing footprint.
        """
        limit = self._quota.max_runtime_replicas
        if limit is None:
            return set()

        occupied = self._occupied_runtime_ids()
        # Already held on an occupied instance: the footprint cannot grow,
        # no steering needed.
        if self._is_already_held() or len(occupied) < limit:
            return set()

        return {hb.runtime_id for hb in self._heartbeats} - occupied

    @property
    def replica_limited(self) -> bool:
        """Whether this acquire is constrained to the tenant's footprint."""
        return bool(self.placement_exclusions())

    def quota_rejection_for_replicas(self) -> QuotaExceededError:
        """The 429 raised when the constrained placement finds no instance."""
        limit = self._quota.max_runtime_replicas
        assert limit is not None  # only reachable when the cap is set
        return QuotaExceededError(
            "runtime replica",
            tenant_id=self._tenant_id,
            limit=limit,
            current=len(self._occupied_runtime_ids()),
            retry_after_seconds=settings.lease_quota_retry_after_seconds,
        )

    # ── measurements ───────────────────────────────────────────────────

    def tenant_lease_usage(self) -> dict[str, int]:
        """Live per-tenant lease counts, for logs and the caller's metrics."""
        prefix = f"{self._tenant_id}/"
        counts: dict[str, int] = {self._tenant_id: 0}
        for hb in self._heartbeats:
            for lease in hb.leases:
                if lease.startswith(prefix):
                    counts[self._tenant_id] += 1
        return counts

    def _held_lease_count(self) -> int:
        """Leases this tenant currently reports held, across all instances."""
        prefix = f"{self._tenant_id}/"
        return sum(
            1
            for hb in self._heartbeats
            for lease in hb.leases
            if lease.startswith(prefix)
        )

    def _is_already_held(self) -> bool:
        """Whether a live instance still reports THIS lease key as held."""
        return any(
            self._lease_key in hb.leases for hb in self._heartbeats
        )

    def _occupied_runtime_ids(self) -> set[str]:
        """Instances currently serving at least one of the tenant's leases."""
        prefix = f"{self._tenant_id}/"
        return {
            hb.runtime_id
            for hb in self._heartbeats
            if any(lease.startswith(prefix) for lease in hb.leases)
        }

    # ── loading ────────────────────────────────────────────────────────

    @staticmethod
    async def _read_quota(client, tenant_id: str) -> t.Optional[TenantQuotaSnapshot]:
        """The tenant's snapshot: its override key, else the platform defaults.

        An unreadable Redis degrades to ``None`` (enforcement off) with a
        warning — see the module docstring's fail-open rule.
        """
        try:
            for key in (tenant_quota_redis_key(tenant_id), quota_defaults_redis_key()):
                raw = await client.get(key)
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                quota = decode_quota_snapshot(raw)
                if quota is not None:
                    return quota
        except Exception as exc:  # noqa: BLE001 — RedisError / OSError
            logger.warning(
                "lease_quota_read_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
        return None


async def _live_heartbeats(client) -> t.Optional[list[RuntimeHeartbeat]]:
    """Every decodable, non-expired heartbeat, or ``None`` when unreadable.

    The index set cannot expire per member, so ids whose heartbeat has gone
    are simply skipped — the same pruning rule placement applies.
    """
    try:
        members = await client.smembers(runtime_index_redis_key())
        heartbeats: list[RuntimeHeartbeat] = []
        for runtime_id in members:
            raw = await client.get(runtime_redis_key(runtime_id))
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode()
            heartbeat = decode_runtime_heartbeat(raw)
            if heartbeat is not None:
                heartbeats.append(heartbeat)
        return heartbeats
    except Exception as exc:  # noqa: BLE001
        logger.warning("lease_quota_heartbeats_unreadable", error=str(exc))
        return None
