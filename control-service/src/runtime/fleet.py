"""The runtime fleet as the control plane sees it (design 03 §3.3, 05 §5.2).

Routing an env-control call needs more than "who holds this one lease". Three
of the runtime's endpoint families are addressed at a WORKSPACE rather than at a
single working copy — "which members have a local root", "read the engine
through any copy", "does this branch exist anywhere" — and in a multi-replica
cell those answers are per-volume facts:

* a union (``git/members``, ``git/worktrees``) is only complete if every
  replica is asked. A member missed here keeps a worktree of an environment the
  registry believes it released;
* an existence probe (``git/first-existing``, ``sqlmesh/state-environments``)
  is only honest if a replica that HAS files answers it. A replica with none
  answers "absent", which the reconciler reads as drift and the registry as
  "nothing to release".

So this module answers "which addresses could hold files for this workspace,
best candidate first" and "every live replica".

Why the heartbeat index rather than a lease scan
------------------------------------------------
Each replica's heartbeat (``rt:{id}``) carries the lease keys its writer
believes it holds, so ONE ``SMEMBERS`` plus one ``GET`` per replica yields both
the full replica set and which of them hold copies of a given workspace. The
alternative — ``SCAN``ning the lease keyspace — costs a pass over a keyspace
that grows with the whole cell, and this runs on paths a user waits for.

Honest degradation
------------------
When Redis is unavailable the fleet is UNKNOWN, not empty. Callers fall back to
the configured Service address: that is the correct answer for a single-replica
deployment (the common one) and the only available answer otherwise. Reporting
an empty fleet instead would make a union read "no members" and an existence
probe read "nothing exists" — the two conclusions that authorize deletions.
"""

from __future__ import annotations

import time
import typing as t
from dataclasses import dataclass, field

from datacode_common.lease import (
    decode_runtime_heartbeat,
    normalize_runtime_addr,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# How stale a fleet view may be. Matches the lease router's cache TTL: the two
# describe the same moving target (which replica holds what), and letting one
# outlive the other would have the holder route and the candidate list disagree.
FLEET_CACHE_TTL_SECONDS = 10.0


@dataclass(frozen=True)
class FleetView:
    """One observation of the runtime fleet.

    ``known`` is False when coordination could not be read at all — distinct
    from "no replicas", which is not a state a running cell can be in.
    """

    known: bool
    #: Dialable base URLs of every live, non-draining replica.
    addrs: tuple[str, ...] = ()
    #: Lease keys each address claims, for workspace attribution.
    leases_by_addr: t.Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def candidates_for_workspace(
        self, tenant_id: str, workspace_id: str
    ) -> tuple[str, ...]:
        """Replicas to ask about a workspace, most likely to have files first.

        Replicas holding one of the workspace's leases come first because they
        certainly have (or are materializing) files for it; the rest follow
        because a working copy can outlive its lease — released, expired, or
        left by a previous holder — and an existence probe that skipped those
        replicas would report files as gone while they are still on disk.
        """
        prefix = f"{tenant_id}/{workspace_id}/"
        holders = [
            addr
            for addr in self.addrs
            if any(key.startswith(prefix) for key in self.leases_by_addr.get(addr, ()))
        ]
        rest = [addr for addr in self.addrs if addr not in holders]
        return tuple(holders + rest)


class RuntimeFleet:
    """Reads the live runtime set from the coordination backend."""

    def __init__(
        self,
        *,
        redis_factory: t.Optional[t.Callable[[], t.Any]] = None,
        cache_ttl: float = FLEET_CACHE_TTL_SECONDS,
    ) -> None:
        # Injectable so tests drive both the "coordination available" and the
        # "coordination down" branches deterministically; a suite running next
        # to a live Redis would otherwise only ever exercise one of them.
        self._redis_factory = redis_factory or _default_client
        self._cache_ttl = cache_ttl
        self._cached: t.Optional[FleetView] = None
        self._cached_until = 0.0

    async def view(self) -> FleetView:
        """The current fleet, cached for :data:`FLEET_CACHE_TTL_SECONDS`.

        No lock guards the cache: an asyncio service mutates it only between
        awaits, and a duplicated read under a race is harmless (both observe
        the same fleet) whereas a lock would serialize every dispatch behind
        one assignment.
        """
        now = time.monotonic()
        if self._cached is not None and now < self._cached_until:
            return self._cached

        view = await self._read()
        if view.known:
            self._cached = view
            self._cached_until = now + self._cache_ttl
        return view

    def invalidate(self) -> None:
        """Drop the cached view (a replica just proved unreachable)."""
        self._cached = None
        self._cached_until = 0.0

    async def _read(self) -> FleetView:
        client = self._redis_factory()
        if client is None:
            # Leases are off in this deployment: a single replica serves
            # everything and the Service address is the whole fleet.
            return FleetView(known=False)

        try:
            members = await client.smembers(runtime_index_redis_key())
        except Exception as exc:  # noqa: BLE001 — connection errors take many shapes
            logger.warning("runtime_fleet_unreadable", error=str(exc))
            return FleetView(known=False)

        addrs: list[str] = []
        leases: dict[str, tuple[str, ...]] = {}
        for member in members or ():
            runtime_id = member.decode() if isinstance(member, bytes) else str(member)
            if not runtime_id:
                continue
            try:
                raw = await client.get(runtime_redis_key(runtime_id))
            except Exception:  # noqa: BLE001 — one unreadable member, not a failed pass
                continue
            if not raw:
                # Expired heartbeat. The index is a SET and cannot expire
                # per-member, so a stale id is normal and simply skipped.
                continue
            if isinstance(raw, bytes):
                raw = raw.decode()
            heartbeat = decode_runtime_heartbeat(raw)
            if heartbeat is None or not heartbeat.addr:
                continue
            if heartbeat.draining:
                # A draining replica keeps its current leases but must not be
                # handed new work, and its files are about to move. Excluded
                # from candidates; the lease route still reaches it for the
                # copies it genuinely still holds.
                continue
            addr = normalize_runtime_addr(heartbeat.addr)
            if addr in leases:
                continue
            addrs.append(addr)
            leases[addr] = tuple(heartbeat.leases)

        if not addrs:
            # The index answered but nothing in it is alive. Treated as UNKNOWN
            # rather than as an empty fleet: some replica is serving this very
            # request, so "no replicas" cannot be true, and acting on it would
            # let a union read report zero members.
            logger.warning(
                "runtime_fleet_empty",
                effect="falling back to the service address",
            )
            return FleetView(known=False)

        return FleetView(known=True, addrs=tuple(addrs), leases_by_addr=leases)


def _default_client():
    """The platform's shared async Redis client, or ``None`` when disabled."""
    from datacode_common.redis import get_redis

    return get_redis()


_fleet: t.Optional[RuntimeFleet] = None


def get_runtime_fleet() -> RuntimeFleet:
    """Get or create the global :class:`RuntimeFleet` (holds the view cache)."""
    global _fleet
    if _fleet is None:
        _fleet = RuntimeFleet()
    return _fleet


def set_runtime_fleet(fleet: t.Optional[RuntimeFleet]) -> None:
    """Replace the global fleet (tests wire a fake here)."""
    global _fleet
    _fleet = fleet


__all__ = ["FleetView", "RuntimeFleet", "get_runtime_fleet", "set_runtime_fleet"]
