"""Lease holder — this instance's side of workspace ownership (design 03 §3.3, P4a).

Responsibilities, in the order they matter:

1. **Prove ownership before touching files.** A lease is only real once BOTH
   the distributed claim (Redis ``SET NX``) and the local claim (``flock``, see
   :mod:`src.lease.mount_guard`) are held. Either alone is insufficient: Redis
   can be unreachable, and ``flock`` cannot see other nodes.
2. **Keep it alive while work runs.** A watchdog renews every ``ttl/3``, so the
   TTL never expires under a long ``apply``.
3. **Give it up cleanly.** Release is compare-and-swap, so a holder whose lease
   already lapsed and was re-granted elsewhere cannot delete its successor's
   claim.

Why every mutation is a CAS on an ownership token
-------------------------------------------------
Without it, this sequence loses a workspace: instance A's lease lapses during a
GC pause, the controller grants it to B, A wakes up and issues ``DEL`` or
``PEXPIRE``. A would be operating on B's claim. Comparing the token first makes
A's late writes no-ops, which is the distributed twin of a generation counter.

The scripts themselves live in :mod:`datacode_common.lease`
(``CAS_DELETE_SCRIPT`` / ``CAS_RENEW_SCRIPT``) because the reconciler's
stale-lease repair needs the identical primitive; a second copy here would be a
second chance for one of them to decay into a blind ``DEL``.

Degradation
-----------
When Redis is unreachable the holder does NOT fabricate a lease. It refuses,
and the gateway falls back to stable-hash routing (design 05 §5.2) — at which
point the ``flock`` guard is the only thing standing between two instances and
one worktree, which is exactly the residual risk the design accepts. Inventing
a lease here would instead make the instance *believe* it is the sole owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import time
import typing as t
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from datacode_common.lease import (
    CAS_DELETE_SCRIPT,
    CAS_RENEW_SCRIPT,
    LEASE_TTL_SECONDS,
    Lease,
    LeaseState,
    lease_redis_key,
    renew_interval_seconds,
)
from datacode_common.naming import split_lease_key

from src.lease.mount_guard import MountGuard, SingleMountViolation, acquire_mount
from src.utils.logging import get_logger

logger = get_logger(__name__)


class LeaseSplitBrain(RuntimeError):
    """The file lock and the distributed lease disagree about the owner.

    Raised when ``flock`` arbitration fails AFTER Redis granted the claim —
    the shape a Redis degradation produces (stable-hash routing picked a
    second writer). Distinct from :class:`LeaseUnavailable` because the
    contract answer differs: 409 LEASE_SPLIT_BRAIN with a caller retry,
    not a 429 capacity back-off.
    """


# Lock contention surfaces as different exception types by platform: the
# guard raises SingleMountViolation when it can name the holder, but the
# underlying flock(2) can also fail directly with EWOULDBLOCK/EAGAIN (macOS
# fcntl) — which is the same fact ("someone else holds the lock") wearing a
# different exception class.
_LOCK_CONTENTION_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})


def _is_lock_contention(exc: BaseException) -> bool:
    """Whether a mount failure means 'another process holds the lock'."""
    if isinstance(exc, SingleMountViolation):
        return True
    return isinstance(exc, OSError) and exc.errno in _LOCK_CONTENTION_ERRNOS


class LeaseUnavailable(RuntimeError):
    """This instance may not serve the working copy.

    Raised both when another instance holds the lease and when the claim cannot
    be established at all (Redis down). Callers must surrender: the gateway
    then retries against the real holder. Serving anyway is what produces two
    writers on one worktree.
    """


def runtime_id() -> str:
    """Stable identity of this runtime instance.

    Prefers the deployment-provided id (a StatefulSet pod name such as
    ``workspace-runtime-2``) because it must survive a restart: leases are
    keyed to an instance, and an identity containing the PID would make every
    restart look like a brand-new instance holding no leases, stranding the
    Redis records of the ones it did hold until their TTL lapsed.
    """
    return (
        os.getenv("DATACODE_RUNTIME_ID")
        or os.getenv("HOSTNAME")
        or os.uname().nodename
    )


class LeaseHolder:
    """Owns the set of working copies this instance is allowed to serve."""

    def __init__(
        self,
        *,
        client,
        materialize_root: str,
        runtime_addr: str,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._materialize_root = materialize_root
        self._runtime_addr = runtime_addr
        self._ttl_seconds = int(ttl_seconds)
        self._runtime_id = runtime_id()
        # lease_key -> (token, MountGuard, watchdog task)
        self._held: dict[str, _HeldLease] = {}

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def held_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._held))

    def holds(self, lease_key: str) -> bool:
        return lease_key in self._held

    async def acquire(self, lease_key: str) -> Lease:
        """Claim a working copy, or raise :class:`LeaseUnavailable`.

        Order is deliberate: the DISTRIBUTED claim is taken first, because it is
        the one the rest of the platform routes on. Taking the local ``flock``
        first would leave a lock held for a workspace this instance turns out
        not to own, blocking the legitimate holder if it lands on this node.
        """
        split_lease_key(lease_key)
        if (existing := self._held.get(lease_key)) is not None:
            # Idempotent: a second request for a lease we already hold is the
            # normal case (every request for that workspace arrives here).
            return existing.lease

        token = uuid.uuid4().hex
        full_key = lease_redis_key(lease_key)
        payload = json.dumps(
            {
                "token": token,
                "runtime_id": self._runtime_id,
                "runtime_addr": self._runtime_addr,
                "acquired_at": time.time(),
            }
        )

        try:
            acquired = await self._client.set(
                full_key, payload, nx=True, px=self._ttl_seconds * 1000
            )
        except Exception as exc:  # RedisError / OSError
            # No lease is fabricated: see module docstring.
            logger.warning(
                "lease_acquire_unavailable", lease_key=lease_key, error=str(exc)
            )
            raise LeaseUnavailable(
                f"cannot establish a lease for {lease_key!r}: coordination "
                "backend unreachable"
            ) from exc

        if not acquired:
            holder = await self._current_holder(full_key)
            logger.info("lease_held_elsewhere", lease_key=lease_key, holder=holder)
            raise LeaseUnavailable(
                f"working copy {lease_key!r} is leased to {holder or 'another instance'}"
            )

        # Local claim second. If it fails, the distributed claim must be given
        # back immediately, otherwise this instance blocks a workspace it
        # cannot actually serve until the TTL lapses.
        try:
            guard = acquire_mount(self._materialize_root, lease_key)
        except (SingleMountViolation, OSError) as exc:
            await self._release_claim(full_key, token, lease_key)
            if _is_lock_contention(exc):
                # Split brain (design 08 §7): another process on this node
                # holds the file lock while Redis just granted US the lease —
                # the exact shape a Redis degradation produces. Distinct from
                # LeaseUnavailable because the contract answer differs: the
                # caller should RETRY (409), not back off for capacity (429).
                logger.warning(
                    "lease_split_brain_detected", lease_key=lease_key, error=str(exc)
                )
                raise LeaseSplitBrain(
                    f"file-lock arbitration failed for {lease_key!r}: another "
                    f"process holds the mount"
                ) from exc
            logger.warning(
                "lease_local_claim_failed", lease_key=lease_key, error=str(exc)
            )
            raise LeaseUnavailable(
                f"cannot mount {lease_key!r} on this instance: {exc}"
            ) from exc

        now = datetime.now(timezone.utc)
        lease = Lease(
            lease_key=lease_key,
            runtime_id=self._runtime_id,
            runtime_addr=self._runtime_addr,
            state=LeaseState.HELD,
            acquired_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        held = _HeldLease(lease=lease, token=token, guard=guard, watchdog=None)
        held.watchdog = asyncio.create_task(
            self._watchdog(lease_key, full_key, token),
            name=f"lease-watchdog-{lease_key}",
        )
        self._held[lease_key] = held
        logger.info(
            "lease_acquired",
            lease_key=lease_key,
            runtime_id=self._runtime_id,
            ttl_seconds=self._ttl_seconds,
        )
        return lease

    async def release(self, lease_key: str) -> None:
        """Give up a lease. Idempotent.

        The local guard is released LAST: dropping it first would let a second
        instance take the flock while this one still owns the Redis claim, i.e.
        exactly the window the guard exists to close.
        """
        held = self._held.pop(lease_key, None)
        if held is None:
            return

        if held.watchdog is not None:
            held.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await held.watchdog

        await self._release_claim(lease_redis_key(lease_key), held.token, lease_key)
        held.guard.release()
        logger.info("lease_released", lease_key=lease_key)

    async def release_all(self) -> None:
        """Release every lease — the drain path (design 06 P4a)."""
        for lease_key in self.held_keys():
            await self.release(lease_key)

    def mark_draining(self, lease_key: str) -> None:
        """Flag a lease as draining while keeping it routable.

        A draining holder still owns the working copy, so requests for
        in-flight operations must keep arriving here; only NEW placements stop.
        See :class:`datacode_common.lease.LeaseState`.
        """
        held = self._held.get(lease_key)
        if held is not None:
            held.lease = _with_state(held.lease, LeaseState.DRAINING)

    @asynccontextmanager
    async def lease(self, lease_key: str) -> t.AsyncIterator[Lease]:
        """Hold a lease for the duration of the block (used by tests/tools).

        Production holds leases across requests, not within one, so this is a
        convenience wrapper rather than the main entry point.
        """
        lease = await self.acquire(lease_key)
        try:
            yield lease
        finally:
            await self.release(lease_key)

    # ── internals ─────────────────────────────────────────────────────

    async def _watchdog(self, lease_key: str, full_key: str, token: str) -> None:
        """Renew while this holder lives.

        Renewing every ttl/3 leaves a full interval of slack after one failed
        renewal, so a brief network blip does not cost the lease. A renewal
        that reports "not owner" means the lease was already re-granted: the
        holder must stop immediately and drop the working copy rather than keep
        writing to files someone else now owns.
        """
        interval = renew_interval_seconds(self._ttl_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._client.eval(
                    CAS_RENEW_SCRIPT, 1, full_key, token, self._ttl_seconds * 1000
                )
            except Exception as exc:  # RedisError / OSError
                logger.warning(
                    "lease_renew_failed", lease_key=lease_key, error=str(exc)
                )
                continue

            if not renewed:
                logger.error("lease_lost", lease_key=lease_key, runtime_id=self._runtime_id)
                # Self-eviction: schedule it rather than awaiting release()
                # from inside the task release() cancels.
                asyncio.create_task(  # noqa: RUF006
                    self._evict(lease_key), name=f"lease-evict-{lease_key}"
                )
                return

            held = self._held.get(lease_key)
            if held is not None:
                held.lease = held.lease.renewed(ttl_seconds=self._ttl_seconds)

    async def _evict(self, lease_key: str) -> None:
        """Drop a lease this instance no longer owns."""
        held = self._held.pop(lease_key, None)
        if held is None:
            return
        held.guard.release()
        logger.warning("lease_evicted", lease_key=lease_key)

    async def _release_claim(self, full_key: str, token: str, lease_key: str) -> None:
        """Compare-and-delete our own claim; never a successor's."""
        try:
            await self._client.eval(CAS_DELETE_SCRIPT, 1, full_key, token)
        except Exception as exc:  # RedisError / OSError
            # Safe to leave behind: the TTL expires it. Logged because it means
            # the workspace is unreachable for up to one TTL.
            logger.warning(
                "lease_release_failed", lease_key=lease_key, error=str(exc)
            )

    async def _current_holder(self, full_key: str) -> str | None:
        """Best-effort read of who holds a contended lease (diagnostics)."""
        try:
            raw = await self._client.get(full_key)
        except Exception:  # noqa: BLE001 - diagnostics only
            return None
        if not raw:
            return None
        try:
            return str(json.loads(raw).get("runtime_id")) or None
        except (ValueError, AttributeError):
            return None


class _HeldLease:
    """Bookkeeping for one lease this instance holds."""

    __slots__ = ("lease", "token", "guard", "watchdog")

    def __init__(
        self,
        *,
        lease: Lease,
        token: str,
        guard: MountGuard,
        watchdog: "asyncio.Task[None] | None",
    ) -> None:
        self.lease = lease
        self.token = token
        self.guard = guard
        self.watchdog = watchdog


def _with_state(lease: Lease, state: LeaseState) -> Lease:
    return replace(lease, state=state)
