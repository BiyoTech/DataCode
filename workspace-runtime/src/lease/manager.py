"""Lease orchestration — the only sanctioned way to reach a working copy.

The four P4a mechanisms are individually testable but individually unsafe:

* :mod:`src.lease.holder` proves ownership, but does not create files;
* :mod:`src.lease.materialize` creates files, and CANNOT check ownership;
* :mod:`src.lease.wip` protects uncommitted work, but must know which copies
  exist;
* :mod:`src.lease.mount_guard` defends the filesystem, but only locally.

Composing them correctly is not optional, and a comment saying "callers must
hold the lease first" is not a mechanism. This module is that mechanism: it
returns a working copy's path ONLY as the result of having acquired its lease,
so code that has a path provably went through ownership to get it.

The ordering invariant
----------------------
    acquire lease  ->  materialize  ->  register for WIP snapshots

Each step is undone if a later one fails, because a partial state is worse than
no state: a lease held for a copy that failed to materialize blocks the
workspace for a full TTL, and a materialized copy nobody registered is
uncommitted work with no protection.

What this module deliberately does NOT do
-----------------------------------------
It does not decide WHERE a lease should live (that is control-service's
placement policy), and it does not read the database. The remote URL and
credentials arrive through ``source_provider`` so that the lease path stays
testable without a database, and so this layer cannot grow a second opinion
about which repository a workspace points at.
"""

from __future__ import annotations

import asyncio
import time
import typing as t
from dataclasses import dataclass, replace

from datacode_common.naming import split_lease_key

from src.git.ops import GitAuth
from src.lease import materialize as materialize_mod
from src.lease import wip
from src.lease.audit import ACTION_ACQUIRE, ACTION_DRAIN, ACTION_RELEASE
from src.lease.heartbeat import HeartbeatReporter
from src.lease.holder import LeaseHolder, LeaseUnavailable
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RepositorySource:
    """Where a workspace's code comes from, resolved by the caller."""

    repo_url: str
    auth: t.Optional[GitAuth] = None


@dataclass(frozen=True)
class ServedCopy:
    """A working copy this instance owns and may serve requests from.

    Possessing one of these is the proof of ownership: it can only be obtained
    from :meth:`LeaseManager.serve`, which acquires the lease first.
    """

    lease_key: str
    workspace_path: str
    repo_path: str
    branch: str
    # True on the request that paid the materialization cost, so first-hit
    # latency can be reported without timing every request.
    freshly_materialized: bool
    # Present when the server held uncommitted work from a previous holder.
    # The USER decides whether to restore it (design 07 Q6): applying it
    # automatically would resurrect edits someone deliberately abandoned.
    pending_snapshot: t.Optional[wip.RecoveredSnapshot] = None

    @property
    def needs_recovery_prompt(self) -> bool:
        return self.pending_snapshot is not None


class LeaseFullError(RuntimeError):
    """This instance cannot accept another lease.

    Placement decisions read heartbeats that may be a whole interval stale, so
    two concurrent placements can pick the same instance. The instance itself is
    therefore the enforcement point (design 07 Q3): the count limit is checked
    HERE, against live state, and the memory watermark is checked by the caller
    (see wiring), not by whoever looked at a heartbeat.
    """


class LeaseManager:
    """Owns the lease -> materialize -> protect pipeline for one instance."""

    def __init__(
        self,
        *,
        holder: LeaseHolder,
        materialize_root: str,
        source_provider: t.Callable[[str], t.Awaitable[RepositorySource]],
        wip_interval_seconds: float,
        shallow_depth: int = 0,
        heartbeat: t.Optional[HeartbeatReporter] = None,
        max_leases: int = 16,
        admission_high_watermark: float = 0.80,
        memory_ratio_provider: t.Optional[t.Callable[[], float]] = None,
        auditor: t.Optional[t.Any] = None,
    ) -> None:
        self._holder = holder
        self._materialize_root = materialize_root
        self._source_provider = source_provider
        self._shallow_depth = shallow_depth
        self._heartbeat = heartbeat
        self._max_leases = max_leases
        self._admission_high_watermark = admission_high_watermark
        self._memory_ratio_provider = memory_ratio_provider
        # Lease lifecycle auditor (``src.lease.audit``); duck-typed so tests
        # can substitute a recorder without the database.
        self._auditor = auditor
        self._served: dict[str, ServedCopy] = {}
        # Wall-clock (epoch) last-use stamp per lease key, fed by serve(): the
        # disk reclaimer orders victims by it. Entries survive lease release on
        # purpose — an idle-but-warm copy is exactly the reclaim candidate the
        # stamp identifies — and the reclaimer falls back to directory mtime
        # for keys unknown since a restart.
        self._last_used: dict[str, float] = {}
        # Serializes concurrent first-touches of the SAME lease: without it two
        # requests arriving together would both try to materialize, and the
        # second would fail on the flock its own instance holds.
        self._locks: dict[str, asyncio.Lock] = {}
        # Credentials resolved during serve() are cached so the snapshot loop
        # can push without an await: `source_provider` is async (it may hit the
        # database), while the scheduler's auth lookup is synchronous by design
        # — it runs inside a worker thread.
        self._auth: dict[str, t.Optional[GitAuth]] = {}
        # Set by begin_drain (the controller's remote orchestration entry, P8)
        # and read by the heartbeat's load provider, so placement stops
        # choosing this instance the moment draining starts rather than a
        # whole interval later.
        self._draining = False
        self._snapshots = wip.SnapshotScheduler(
            interval_seconds=wip_interval_seconds,
            lease_provider=self._snapshot_targets,
            auth_provider=self._auth.get,
        )

    @property
    def draining(self) -> bool:
        """Whether this instance has stopped accepting new leases.

        Reported through the heartbeat (``draining`` field): placement and the
        tenant quota guard both treat a draining instance as unavailable.
        """
        return self._draining

    def holds(self, lease_key: str) -> bool:
        """Whether this instance currently holds ``lease_key``.

        The request-path ownership check (``src.api.deps``) reads this to
        answer ``409 LEASE_NOT_HELD``: with the lease subsystem on, a request
        for a working copy this replica does not hold means the gateway's
        cached route is stale, and the honest answer is "not here" rather
        than serving a copy this volume may not contain.
        """
        return self._holder.holds(lease_key)

    def attach_heartbeat(self, heartbeat: HeartbeatReporter) -> None:
        """Wire the heartbeat after construction.

        Two-step because the reporter's load provider reads the manager's lease
        count, so neither can be fully built before the other. Must be called
        before :meth:`start`, otherwise the instance never becomes placeable.
        """
        self._heartbeat = heartbeat

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background work (snapshots, heartbeat)."""
        await self._snapshots.start()
        if self._heartbeat is not None:
            await self._heartbeat.start()

    async def drain(self) -> None:
        """Give up everything gracefully — the zero-loss shutdown path.

        Order matters and is the mirror of acquisition:

        1. stop attracting new leases (heartbeat off) — otherwise the controller
           keeps placing work on an instance that is going away;
        2. take a FINAL snapshot of every copy — this is what makes a planned
           shutdown lose nothing rather than up to one snapshot interval;
        3. release the leases, so another instance can take over immediately
           instead of waiting a TTL.

        Safe to call twice (the shutdown hook after a controller-initiated
        drain): the second pass finds no served leases and stopped tasks.
        """
        self._draining = True
        if self._heartbeat is not None:
            await self._heartbeat.stop()
        await self._snapshots.stop(final_snapshot=True)
        # Audit BEFORE the release: drain is the shutdown path, so this row is
        # awaited inline (unlike acquire/release) — losing it to a racing loop
        # shutdown would defeat the point of an append-only shutdown record.
        for lease_key in self.served_keys():
            await self._audit_record(lease_key, ACTION_DRAIN, "drain")
        # Tell the frontend each copy is moving (design 03 §4.3 step 2d):
        # without this, the drain surfaces as the next request's failure
        # instead of a "relocating" banner and an automatic retry.
        await self._publish_relocating()
        await self._holder.release_all()
        self._served.clear()
        # Credentials must not outlive the leases they belong to.
        self._auth.clear()
        logger.info("lease_manager_drained")

    async def _publish_relocating(self) -> None:
        """Emit ``workspace_relocating`` SSE events for every served copy.

        Best-effort by design: the event bus may be mid-shutdown itself (this
        runs on the drain path), and a failed notification must never block
        the release that follows — the 503 WORKSPACE_RELOCATING fallback on
        the request path covers clients that miss the event.
        """
        try:
            from src.sqlmesh.event_bus import EventTopic, SSEEvent, get_event_bus

            bus = get_event_bus()
            for lease_key in self.served_keys():
                served = self._served.get(lease_key)
                await bus.publish(SSEEvent(
                    topic=EventTopic.WORKSPACE,
                    data={
                        "change": "workspace_relocating",
                        "lease_key": lease_key,
                        "reason": "runtime draining",
                    },
                    workspace_path=served.workspace_path if served else "",
                ))
        except Exception as exc:  # noqa: BLE001 — notification only
            logger.warning(
                "relocating_notification_failed",
                error=str(exc),
                effect="clients will see the 503 fallback instead of the banner",
            )

    async def begin_drain(self) -> None:
        """Announce draining, then drain — the controller's remote entry (P8).

        Split from :meth:`drain` because announcement must not wait for the
        (potentially slow) final snapshots: the draining heartbeat has to be
        visible to placement IMMEDIATELY, or leases keep being routed here
        while the drain is in progress.
        """
        if self._draining:
            return
        self._draining = True
        if self._heartbeat is not None:
            # One immediate publish carries draining=true now; the loop (and
            # its eventual stop inside drain()) takes over from there.
            await self._heartbeat.publish_once()
        await self.drain()

    # ── the request path ──────────────────────────────────────────────

    async def serve(self, lease_key: str) -> ServedCopy:
        """Return the working copy for ``lease_key``, acquiring what is needed.

        Idempotent and cheap on the warm path: an already-served copy short
        circuits before any lock or IO.

        Raises:
            LeaseUnavailable: another instance owns this working copy, or the
                lease could not be established (Redis unreachable). Callers must
                surrender so the gateway can retry against the real holder.
            MaterializationError: the lease was obtained but the files could not
                be produced. The lease is released before this propagates.
        """
        split_lease_key(lease_key)

        if (warm := self._served.get(lease_key)) is not None:
            self._last_used[lease_key] = time.time()
            return warm

        async with self._lock_for(lease_key):
            # Re-check: another coroutine may have completed while we waited.
            if (warm := self._served.get(lease_key)) is not None:
                return warm

            # Local admission control (design 07 Q3). Placement read a
            # heartbeat that may be a full interval stale, so the count is
            # enforced HERE against live state; the memory watermark guards
            # against the per-lease estimate being wrong, which a count can
            # never catch. Checked inside the lock and against the just-fetched
            # served set, so concurrent acquires cannot each see "one slot
            # left" and all proceed.
            self._assert_admission()

            # 1. Ownership FIRST. Everything below writes to disk.
            lease = await self._holder.acquire(lease_key)
            self._audit_soon(lease_key, ACTION_ACQUIRE, "serve")

            try:
                return await self._complete_serving(lease_key, lease)
            except Exception:
                # Undo the lease: holding one for a copy we cannot serve blocks
                # the workspace for a whole TTL and denies it to the instance
                # that could.
                await self._holder.release(lease_key)
                self._audit_soon(lease_key, ACTION_RELEASE, "materialize_failed")
                raise

    async def adopt(self, lease_key: str) -> None:
        """Take a lease NOW; materialize in the background.

        The ``/internal/leases/accept`` path. Split from :meth:`serve` because
        the two callers need different amounts of the pipeline:

        * a forwarded REQUEST needs the files and waits for them (``serve``);
        * the PLACING instance needs only the handshake — the lease must be
          committed (``SET NX`` + flock) before "accepted" is answered, or a
          race would let the placer report success for a lease that was never
          taken. The files, which can take minutes for a large repository,
          continue in the background.

        The per-lease lock is held until the background work finishes, so a
        request arriving in the meantime blocks exactly as long as it would
        have under ``serve`` — the first request pays the remaining cost, per
        the design's ``warmup: in_progress`` contract.
        """
        if self._served.get(lease_key) is not None:
            return

        lock = self._lock_for(lease_key)
        await lock.acquire()
        try:
            if self._served.get(lease_key) is not None:
                return
            self._assert_admission()
            lease = await self._holder.acquire(lease_key)
            self._audit_soon(lease_key, ACTION_ACQUIRE, "adopt")
        except BaseException:
            lock.release()
            raise

        task = asyncio.create_task(
            self._adopt_in_background(lease_key, lease, lock),
            name=f"lease-adopt-{lease_key}",
        )
        task.add_done_callback(lambda t: _log_background_failure(t, lease_key))

    async def _adopt_in_background(
        self,
        lease_key: str,
        lease,
        lock: asyncio.Lock,
    ) -> None:
        """Finish an adopted lease's materialization, then free the lock."""
        try:
            await self._complete_serving(lease_key, lease)
        except Exception as exc:
            # Same rollback rule as serve(): a lease for a copy that cannot be
            # materialized blocks the workspace for a full TTL.
            await self._holder.release(lease_key)
            self._audit_soon(lease_key, ACTION_RELEASE, "adopt_failed")
            logger.error(
                "lease_adopt_failed", lease_key=lease_key, error=str(exc)
            )
        finally:
            lock.release()

    async def _complete_serving(self, lease_key: str, lease):
        """Steps 2-3 (materialize, protect) and the served-set update."""
        try:
            served = await self._materialize_and_protect(lease_key)
        except Exception:
            self._auth.pop(lease_key, None)
            raise
        self._served[lease_key] = served
        self._last_used[lease_key] = time.time()
        logger.info(
            "lease_served",
            lease_key=lease_key,
            runtime_id=lease.runtime_id,
            fresh=served.freshly_materialized,
            pending_snapshot=served.needs_recovery_prompt,
        )
        return served

    async def release(self, lease_key: str) -> None:
        """Release one working copy, snapshotting it first.

        The snapshot precedes the release for the same reason drain snapshots
        before releasing: once the lease is gone another instance may
        materialize the copy, and any unprotected edits become unreachable.
        """
        served = self._served.pop(lease_key, None)
        if served is None:
            await self._holder.release(lease_key)
            return

        # Snapshot THIS copy only. Calling snapshot_all() here would also push
        # every other lease on the instance, turning one release into N pushes.
        # Skipped when a recovery decision is pending, which would otherwise
        # overwrite the snapshot the user has not yet chosen to restore.
        if served.pending_snapshot is None:
            source = await self._source_provider(lease_key)
            await asyncio.to_thread(
                wip.take_snapshot,
                lease_key=lease_key,
                workspace_path=served.workspace_path,
                repo_path=served.repo_path,
                auth=source.auth,
            )

        await self._holder.release(lease_key)
        self._audit_soon(lease_key, ACTION_RELEASE, "explicit")
        self._auth.pop(lease_key, None)
        logger.info("lease_manager_released", lease_key=lease_key)

    def served_copy(self, lease_key: str) -> t.Optional[ServedCopy]:
        """The copy for ``lease_key`` if this instance is already serving it."""
        return self._served.get(lease_key)

    def served_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._served))

    def last_used(self) -> dict[str, float]:
        """Epoch last-use stamps per lease key, for the disk reclaimer's ordering."""
        return dict(self._last_used)

    # ── WIP recovery decisions (driven by the user) ────────────────────

    async def recover_snapshot(self, lease_key: str) -> bool:
        """Apply the pending snapshot for a served copy, on user request."""
        served = self._require_served(lease_key)
        if served.pending_snapshot is None:
            return False

        applied = await asyncio.to_thread(
            wip.apply_snapshot,
            workspace_path=served.workspace_path,
            snapshot=served.pending_snapshot,
        )
        if applied:
            await self._clear_pending_snapshot(lease_key, served)
        return applied

    async def discard_snapshot(self, lease_key: str) -> bool:
        """Drop the pending snapshot for a served copy, on user request."""
        served = self._require_served(lease_key)
        if served.pending_snapshot is None:
            return False

        source = await self._source_provider(lease_key)
        discarded = await asyncio.to_thread(
            wip.discard_snapshot,
            lease_key=lease_key,
            repo_path=served.repo_path,
            auth=source.auth,
        )
        if discarded:
            await self._clear_pending_snapshot(lease_key, served)
        return discarded

    # ── observability ─────────────────────────────────────────────────

    def protection_status(self) -> dict:
        """WIP protection state, for /health and the frontend indicator."""
        return {
            "served": len(self._served),
            "unprotected": list(self._snapshots.unprotected_leases()),
            "last_protected_at": {
                key: stamp.isoformat()
                for key in self._served
                if (stamp := self._snapshots.last_protected_at(key)) is not None
            },
        }

    def copy_protection_status(self, lease_key: str) -> t.Optional[dict]:
        """WIP protection state of ONE working copy (design 07 Q6).

        Feeds the frontend's per-workspace "last protected at" indicator and
        degradation banner. ``None`` means this instance is not serving the
        copy, so the caller can only answer "unavailable" — never a status
        belonging to some other instance.

        Read-only by design: it must be safe to poll frequently, and a status
        query that could mutate anything (acquire, materialize) would make the
        indicator's traffic a lease-management side channel.
        """
        served = self._served.get(lease_key)
        if served is None:
            return None
        return {
            "protected": self._snapshots.is_protected(lease_key),
            "last_protected_at": self._snapshots.last_protected_at(lease_key),
            "pending_snapshot": served.pending_snapshot,
        }

    # ── internals ─────────────────────────────────────────────────────

    def _audit_soon(self, lease_key: str, action: str, reason: str = "") -> None:
        """Schedule an audit row off the request path (acquire/release)."""
        if self._auditor is not None:
            self._auditor.record_soon(lease_key, action, reason)

    async def _audit_record(self, lease_key: str, action: str, reason: str = "") -> None:
        """Await an audit row inline (drain: the shutdown path loses nothing)."""
        if self._auditor is not None:
            await self._auditor.record(lease_key, action, reason)

    def _assert_admission(self) -> None:
        """Refuse one more lease when this instance is at its limits.

        Two checks, both required (design 07 Q3): the count bounds the number of
        small workspaces one instance can accumulate, and the memory watermark
        is what actually prevents an OOM when the per-lease estimate is wrong —
        an OOM kills every OTHER lease on the instance too, which is why a
        wrong estimate must surface as a refused lease rather than a crash.
        """
        if 0 < self._max_leases <= len(self._served):
            raise LeaseFullError(
                f"instance holds {len(self._served)} leases "
                f"(max {self._max_leases})"
            )
        if self._memory_ratio_provider is not None:
            ratio = self._memory_ratio_provider()
            if ratio >= self._admission_high_watermark:
                raise LeaseFullError(
                    f"memory at {ratio:.0%} of budget "
                    f"(watermark {self._admission_high_watermark:.0%})"
                )

    async def _materialize_and_protect(self, lease_key: str) -> ServedCopy:
        """Steps 2 and 3, with the lease already held."""
        source = await self._source_provider(lease_key)
        # Cached for the snapshot loop, which cannot await a provider.
        self._auth[lease_key] = source.auth

        copy = await asyncio.to_thread(
            materialize_mod.materialize,
            materialize_root=self._materialize_root,
            lease_key=lease_key,
            repo_url=source.repo_url,
            auth=source.auth,
            shallow_depth=self._shallow_depth,
        )

        # Look for work a previous holder left behind. Only meaningful on a
        # fresh materialization: a warm copy's edits are already on this disk,
        # and prompting for them would offer to overwrite newer work with older.
        pending = None
        if copy.freshly_materialized:
            pending = await asyncio.to_thread(
                wip.find_snapshot,
                lease_key=lease_key,
                repo_path=copy.repo_path,
                auth=source.auth,
            )

        return ServedCopy(
            lease_key=lease_key,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
            branch=copy.branch,
            freshly_materialized=copy.freshly_materialized,
            pending_snapshot=pending,
        )

    def _snapshot_targets(self) -> t.Iterable[tuple[str, str, str]]:
        """Copies the scheduler should protect.

        A copy awaiting a recovery decision is EXCLUDED: snapshotting it would
        overwrite the very snapshot the user has not yet chosen to restore.
        """
        return [
            (served.lease_key, served.workspace_path, served.repo_path)
            for served in self._served.values()
            if served.pending_snapshot is None
        ]

    async def _clear_pending_snapshot(
        self, lease_key: str, served: ServedCopy
    ) -> None:
        """Mark the recovery decision as made, re-enabling snapshots."""
        self._served[lease_key] = replace(served, pending_snapshot=None)

    def _require_served(self, lease_key: str) -> ServedCopy:
        served = self._served.get(lease_key)
        if served is None:
            raise LeaseUnavailable(
                f"this instance is not serving {lease_key!r}"
            )
        return served

    def _lock_for(self, lease_key: str) -> asyncio.Lock:
        """Per-lease lock, created on first use and dropped when unused.

        Pruning matters: an instance sees thousands of distinct lease keys over
        its lifetime, and a dict that only ever grows is a slow leak.
        """
        lock = self._locks.get(lease_key)
        if lock is None:
            # Opportunistically forget locks nobody is waiting on. Cheap because
            # the dict only holds keys touched since the last prune.
            for key, existing in list(self._locks.items()):
                if key not in self._served and not existing.locked():
                    del self._locks[key]
            lock = asyncio.Lock()
            self._locks[lease_key] = lock
        return lock


def _log_background_failure(task: asyncio.Task, lease_key: str) -> None:
    """Surface a background failure nobody awaits.

    An unobserved task exception produces no log line; the only symptom would
    be a lease that exists but serves nothing.
    """
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        logger.error("lease_background_task_failed", lease_key=lease_key, error=str(exc))
