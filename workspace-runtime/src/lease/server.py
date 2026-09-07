"""Lease placement service — the runtime's side of the acquire flow (design 03 §3.3).

This is the "CTL" box from the design sequence, temporarily co-located on the
runtime instances (the design assigns it to control-service once P5 extracts
it; the decision logic in :mod:`src.lease.placement` moves unchanged).

Flow, and why each hop exists::

    gateway  --miss-->  ANY runtime (load balanced)
    that runtime       picks the emptiest instance (placement.py)
                       ├─ winner is self  → adopt locally
                       └─ winner is other → POST to winner's accept endpoint
    winner             SET NX + flock now; materialization in background
    gateway  <-------- {runtime_id, addr}

Why the winner commits the lease rather than the placing instance
-----------------------------------------------------------------
A lease is only real once its holder has the flock and will hold the files;
having the placing instance write the Redis record would create a lease whose
holder never agreed to hold it — the exact state a crash then strands for a
full TTL. The handshake (``adopt``) is therefore synchronous: by the time
"accepted" is answered, the SET NX has committed.

Why materialization continues in the background
-----------------------------------------------
A large repository takes minutes to clone, but the design returns
``202 {warmup: in_progress}`` precisely so the gateway is not blocked: the
FIRST request for that working copy pays the remaining cost. Correctness is
preserved by the per-lease lock inside :meth:`LeaseManager.adopt` — a request
arriving meanwhile waits for the in-flight materialization instead of racing
it.

Retries
-------
If the winner refuses (lease raced to another instance, flock contention,
capacity), placement re-runs with the refuser EXCLUDED. Placement is
deterministic, so without the exclusion a retry would pick the same instance
again — turning a retry into a hammer on one unlucky box.
"""

from __future__ import annotations

import httpx
from datacode_common.naming import split_lease_key

from src.config import settings
from src.lease.holder import LeaseSplitBrain, LeaseUnavailable, runtime_id
from src.lease.manager import LeaseFullError, LeaseManager
from src.lease.placement import NoInstanceAvailable, Placement, pick_instance
from src.lease.quotas import TenantCapacityGuard
from src.utils.logging import get_logger

logger = get_logger(__name__)

# How long the placing instance waits for the winner's acknowledgement. The
# accept endpoint returns BEFORE materialization, so this bounds only the
# lease-taking handshake (Redis SET NX + flock), which is sub-second; anything
# longer is an unreachable peer, not a slow repository.
ACCEPT_TIMEOUT_SECONDS = 10.0

# One retry round: enough to survive a deterministic mis-pick, few enough that
# a systematically broken instance does not get hammered N times per request.
PLACEMENT_ATTEMPTS = 2


class LeaseAcquireError(RuntimeError):
    """A lease could not be placed on any instance."""


class LeaseServer:
    """Serves acquire/release for the gateway via the local manager."""

    def __init__(
        self,
        *,
        manager: LeaseManager,
        client,
        internal_token: str,
        runtime_addr: str,
    ) -> None:
        self._manager = manager
        self._client = client
        self._token = internal_token
        self._runtime_addr = runtime_addr

    @property
    def addr(self) -> str:
        """This instance's dial address, as the gateway should use it."""
        return self._runtime_addr

    @property
    def holder_runtime_id(self) -> str:
        return runtime_id()

    async def acquire(self, lease_key: str) -> dict:
        """Place a lease and return ``{runtime_id, addr}`` for routing."""
        tenant_id, _workspace_id, _user_id, _environment = split_lease_key(lease_key)

        # Tenant capacity FIRST (P8): a 429 here answers the caller before any
        # placement work, and a quota verdict must not depend on which
        # instance happened to win. ``None`` = enforcement off for this call.
        guard = await TenantCapacityGuard.load(self._client, lease_key)
        if guard is not None:
            guard.assert_within_lease_budget()

        excluded: set[str] = set()
        # Replica steering: at the tenant's replica cap, only its existing
        # instances may win — the lease must consolidate, not spread.
        constrained_to_footprint = guard is not None and guard.replica_limited
        last_error: Exception | None = None

        for _attempt in range(PLACEMENT_ATTEMPTS):
            try:
                placement = await pick_instance(
                    self._client,
                    tenant_id=tenant_id,
                    exclude=excluded
                    | (guard.placement_exclusions() if guard else set()),
                )
            except NoInstanceAvailable as exc:
                if constrained_to_footprint:
                    # Placement was restricted to the tenant's occupied
                    # instances and none of them can take it (full, draining,
                    # refused): the tenant's runtime footprint is full, which
                    # is a quota verdict, not a placement failure.
                    raise guard.quota_rejection_for_replicas() from exc
                raise LeaseAcquireError(str(exc)) from exc

            try:
                addr = await self._delegate(placement, lease_key)
            except (LeaseUnavailable, LeaseFullError, LeaseSplitBrain) as exc:
                # The winner refused. Exclude and re-place rather than fail:
                # placement read stale data, the policy is not wrong in
                # principle. A split brain refuses the same way — the files
                # are in use on that node, so another instance must serve.
                excluded.add(placement.runtime_id)
                last_error = exc
                logger.warning(
                    "lease_placement_refused",
                    lease_key=lease_key,
                    runtime_id=placement.runtime_id,
                    error=str(exc),
                )
                continue

            return {"runtime_id": placement.runtime_id, "addr": addr}

        raise LeaseAcquireError(
            f"no instance accepted the lease for {lease_key!r}: {last_error}"
        ) from last_error

    async def adopt_locally(self, lease_key: str) -> None:
        """Take a placed lease on THIS instance (the accept endpoint).

        Raises:
            LeaseUnavailable: another instance won the lease meanwhile.
            LeaseFullError: this instance is at capacity (stale heartbeat).
        """
        await self._manager.adopt(lease_key)

    async def release(self, lease_key: str) -> dict:
        """Release a lease this instance holds (the gateway's failure path)."""
        await self._manager.release(lease_key)
        return {"released": lease_key}

    # ── internals ─────────────────────────────────────────────────────

    async def _delegate(self, placement: Placement, lease_key: str) -> str:
        """Have the chosen instance take the lease; return its dial address."""
        if placement.runtime_id == runtime_id():
            await self.adopt_locally(lease_key)
            return self._runtime_addr

        # Another instance won placement. It must commit the lease itself
        # (see module docstring); this instance only forwards the request.
        url = f"http://{placement.addr.rstrip('/')}/internal/leases/accept"
        async with httpx.AsyncClient(timeout=ACCEPT_TIMEOUT_SECONDS) as http:
            response = await http.post(
                url,
                json={"lease_key": lease_key},
                headers={"X-Internal-Token": self._token},
            )
        if response.status_code == 409:
            # The winner refused: lease raced elsewhere, or it is full. Both
            # mean "re-place without me", which is what the caller excludes on.
            raise LeaseUnavailable(
                f"{placement.runtime_id} refused {lease_key!r}: "
                f"{response.text[:200]}"
            )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("addr") or placement.addr)


def build_lease_server(manager: LeaseManager, client) -> LeaseServer | None:
    """Assemble the server, or return None when its inputs are missing.

    ``client`` is the platform's async Redis client: placement reads the
    index and heartbeats through it on the request path.
    """
    if not settings.internal_api_enabled:
        logger.warning(
            "lease_server_disabled",
            reason="no internal token configured; the gateway cannot place leases",
        )
        return None
    return LeaseServer(
        manager=manager,
        client=client,
        internal_token=settings.internal_token,
        runtime_addr=f"{runtime_id()}:{settings.rest_port}",
    )
