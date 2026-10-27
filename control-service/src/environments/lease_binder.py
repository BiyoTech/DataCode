"""Lease binder — the reconciler's FIFTH party (design 05 §5.1).

The other four parties are reached through HTTP binders because they live
behind a service (Git and the SQLMesh engine inside workspace-runtime, the
deployments inside its Dagster plane). The lease party is different: its source
of truth is Redis, which the control plane already speaks for its environment
locks. So this binder reads Redis DIRECTLY — no runtime hop — which is what the
design prescribes ("直读 Redis") and what keeps the reconciler able to detect a
dead holder's leftovers even when the runtime that wrote them is gone.

What "reading the lease party" means
------------------------------------
Two independent views, and the drift kinds come from comparing them:

* the LEASES themselves (``lease:*``), each naming the runtime that claims it;
* the runtime HEARTBEATS (``rt:*``), each listing the leases its writer
  believes it holds.

A lease whose holder has no heartbeat is stale; a lease no registry row
accounts for is an orphan; a lease claimed by one runtime while a DIFFERENT
runtime's heartbeat also lists it is a split brain. None of those three is
visible from one view alone, which is why both are read in a single pass and
handed to the reconciler together.

Honest scope
------------
``None`` returns mean "could not read", never "nothing exists" — the same rule
:meth:`SqlMeshBinder.list_state_environments` follows, and for the same reason:
a Redis blip must not make the reconciler declare every lease an orphan and
delete them all.

Repairs are compare-and-swap by construction: this binder never issues a blind
``DEL``. It deletes a lease only while that lease still carries the token this
pass observed, so a holder that legitimately took over between the read and the
write keeps its claim.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone

from datacode_common.lease import (
    CAS_DELETE_SCRIPT,
    LeaseRecord,
    RuntimeHeartbeat,
    decode_lease_record,
    decode_runtime_heartbeat,
    lease_key_of_redis_key,
    lease_redis_key,
    lease_redis_key_pattern,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# How many keys one SCAN iteration asks for. Leases are counted in hundreds per
# cell, so this bounds the pass at a handful of round trips without ever issuing
# the blocking KEYS command.
_SCAN_COUNT = 200

# Upper bound on keys examined in one pass. A runaway keyspace (a bug elsewhere,
# a mis-prefixed writer) must not turn reconciliation into an unbounded loop
# that starves the rest of the control plane; the pass is reported as truncated
# instead, which is a fact an operator can act on.
_MAX_KEYS_PER_PASS = 20_000


@dataclass(frozen=True)
class LeaseWorld:
    """One snapshot of the lease party, as a single pass observed it.

    Both views are captured together on purpose: comparing a lease read now
    against a heartbeat read a minute ago would manufacture split brains out of
    ordinary hand-overs.
    """

    leases: t.Mapping[str, LeaseRecord] = field(default_factory=dict)
    heartbeats: t.Mapping[str, RuntimeHeartbeat] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # True when the pass hit `_MAX_KEYS_PER_PASS`. A truncated pass may not
    # conclude "this lease has no owner" for the keys it never reached, so the
    # reconciler downgrades itself to reporting only.
    truncated: bool = False

    def live_runtime_ids(self) -> frozenset[str]:
        """Runtimes whose heartbeat is present, i.e. that are demonstrably alive."""
        return frozenset(self.heartbeats)

    def claimants_of(self, lease_key: str) -> tuple[str, ...]:
        """Every runtime that believes it holds ``lease_key``.

        Includes the lease record's own ``runtime_id`` and any heartbeat that
        lists the key. More than one entry IS the split-brain condition: the
        design's invariant I1 says a lease key has at most one mounting writer.
        """
        claimants: list[str] = []
        record = self.leases.get(lease_key)
        if record is not None:
            claimants.append(record.runtime_id)
        for heartbeat in self.heartbeats.values():
            if lease_key in heartbeat.leases and heartbeat.runtime_id not in claimants:
                claimants.append(heartbeat.runtime_id)
        return tuple(claimants)


class LeaseBinder:
    """Reads and (on request) reclaims the lease party's state."""

    def __init__(self, *, client_factory: t.Optional[t.Callable[[], t.Any]] = None) -> None:
        # Injectable so the reconciler's tests drive a fake client. Resolved per
        # call rather than stored: the shared client is bound to the running
        # event loop, and holding one across loops is what breaks test suites.
        self._client_factory = client_factory or _default_client

    async def read_world(self) -> t.Optional[LeaseWorld]:
        """Snapshot leases and heartbeats, or ``None`` when Redis is unreadable."""
        client = self._client_factory()
        if client is None:
            # Redis disabled: leases do not exist in this deployment, so there
            # is no fifth party to reconcile. Distinct from "unreachable" —
            # returning None here would report a permanent outage in a
            # single-instance dev topology that simply has no leases.
            logger.debug("lease_world_unavailable", reason="redis disabled")
            return None

        try:
            leases, truncated = await self._read_leases(client)
            heartbeats = await self._read_heartbeats(client)
        except Exception as exc:  # noqa: BLE001 — unreadable, not empty
            logger.warning("lease_world_read_failed", error=str(exc))
            return None

        world = LeaseWorld(
            leases=leases,
            heartbeats=heartbeats,
            observed_at=datetime.now(timezone.utc),
            truncated=truncated,
        )
        logger.info(
            "lease_world_read",
            leases=len(leases),
            live_runtimes=len(heartbeats),
            truncated=truncated,
        )
        return world

    async def list_for_workspace(
        self, *, tenant_id: str, workspace_id: str
    ) -> t.Optional[list[LeaseRecord]]:
        """Leases of one workspace, or ``None`` when Redis is unreadable.

        ``None`` means "no verdict", never "none exist" — the same honesty rule
        :meth:`read_world` follows. It matters more here: a workspace deletion
        that reported the leases released while Redis was merely unreachable
        would leave a live writer holding a working copy the deletion is about to
        remove, and the next workspace of the same name would inherit it.
        """
        client = self._client_factory()
        if client is None:
            # Redis disabled: leases do not exist in this deployment. Distinct
            # from unreachable — there is nothing to release, which is a verdict.
            return []

        prefix = _workspace_lease_prefix(tenant_id, workspace_id)
        try:
            leases, truncated = await self._read_leases(client)
        except Exception as exc:  # noqa: BLE001 — unreadable, not empty
            logger.warning(
                "workspace_lease_scan_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            return None
        if truncated:
            # A truncated pass may not conclude "these are all of them".
            logger.warning("workspace_lease_scan_truncated", workspace_id=workspace_id)
            return None

        return [
            record
            for key, record in leases.items()
            if key.startswith(prefix)
        ]

    async def release(self, record: LeaseRecord) -> bool:
        """Release one lease, but only while it still carries ``record``'s token.

        Compare-and-swap, never a blind ``DEL``: between the scan and this write
        the lease may have been re-acquired by a runtime that legitimately holds
        the working copy now, and deleting its claim would let a second writer
        mount the same copy.
        """
        return await self.release_stale(record)

    async def release_stale(self, record: LeaseRecord) -> bool:
        """Delete a lease, but only while it still carries ``record``'s token.

        Returns whether the delete actually removed the key. False means the
        lease was already gone or has been re-acquired since it was read — both
        are correct outcomes for a repair whose premise ("this claim is dead")
        no longer holds.
        """
        client = self._client_factory()
        if client is None:
            return False
        try:
            removed = await client.eval(
                CAS_DELETE_SCRIPT,
                1,
                lease_redis_key(record.lease_key),
                record.token,
            )
        except Exception as exc:  # noqa: BLE001 — the TTL is the backstop
            logger.warning(
                "stale_lease_release_failed",
                lease_key=record.lease_key,
                runtime_id=record.runtime_id,
                error=str(exc),
            )
            return False

        released = bool(removed)
        logger.info(
            "stale_lease_released" if released else "stale_lease_release_superseded",
            lease_key=record.lease_key,
            runtime_id=record.runtime_id,
        )
        return released

    # ── internals ──────────────────────────────────────────────────────

    async def _read_leases(
        self, client
    ) -> tuple[dict[str, LeaseRecord], bool]:
        """Every decodable lease of this cell, keyed by lease key.

        SCAN, never KEYS: this runs inside a periodic control loop against the
        same Redis the request path uses, and KEYS blocks the server for the
        duration of the scan.
        """
        records: dict[str, LeaseRecord] = {}
        pattern = lease_redis_key_pattern()
        cursor = 0
        examined = 0

        while True:
            cursor, keys = await client.scan(
                cursor=cursor, match=pattern, count=_SCAN_COUNT
            )
            for raw_key in keys:
                examined += 1
                key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
                lease_key = lease_key_of_redis_key(key)
                if not lease_key:
                    # A key matching the prefix whose suffix is not a lease key:
                    # written by another version or another domain. Skipped, not
                    # deleted — this binder only reclaims what it understands.
                    logger.warning("lease_scan_unparseable_key", redis_key=key)
                    continue
                raw = await client.get(key)
                if not raw:
                    # Expired between SCAN and GET. Nothing to reconcile: the
                    # TTL did exactly what it exists for.
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                record = decode_lease_record(lease_key, raw)
                if record is None:
                    logger.warning("lease_record_undecodable", lease_key=lease_key)
                    continue
                records[lease_key] = record

            if int(cursor) == 0:
                return records, False
            if examined >= _MAX_KEYS_PER_PASS:
                logger.warning(
                    "lease_scan_truncated",
                    examined=examined,
                    limit=_MAX_KEYS_PER_PASS,
                    effect="this pass reports lease drift but repairs none",
                )
                return records, True

    async def _read_heartbeats(self, client) -> dict[str, RuntimeHeartbeat]:
        """Every decodable heartbeat behind the runtime index.

        The index is authoritative for "which runtimes exist"; a member whose
        heartbeat key has expired is simply absent from the result, which is the
        signal ``stale_lease`` is built on.
        """
        heartbeats: dict[str, RuntimeHeartbeat] = {}
        members = await client.smembers(runtime_index_redis_key())
        for member in members or ():
            runtime_id = member.decode() if isinstance(member, bytes) else str(member)
            raw = await client.get(runtime_redis_key(runtime_id))
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode()
            heartbeat = decode_runtime_heartbeat(raw)
            if heartbeat is not None:
                heartbeats[heartbeat.runtime_id] = heartbeat
        return heartbeats


def _workspace_lease_prefix(tenant_id: str, workspace_id: str) -> str:
    """Lease-key prefix selecting every working copy of one workspace.

    A lease key is ``{tenant}/{workspace}/{user}/{environment}``, so this is its
    first two segments. The TRAILING SLASH is load-bearing: without it workspace
    ``ws-shop`` would also match every lease of ``ws-shop-archive``, and deleting
    one workspace would evict another's live writers.
    """
    return f"{tenant_id}/{workspace_id}/"


def _default_client():
    """The platform's shared async Redis client, or ``None`` when disabled."""
    from datacode_common.redis import get_redis

    return get_redis()


_binder: t.Optional[LeaseBinder] = None


def get_lease_binder() -> LeaseBinder:
    """Get or create the global :class:`LeaseBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = LeaseBinder()
    return _binder


__all__ = ["LeaseBinder", "LeaseWorld", "get_lease_binder"]
