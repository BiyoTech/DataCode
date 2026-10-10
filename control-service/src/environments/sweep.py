"""Periodic reconciliation sweep — the loop that makes drift detection matter.

Until now ``reconcile_workspace`` existed but had no production caller: drift was
only found when a user opened an environment and clicked reconcile. That is
adequate for the four original parties, because each of them is repaired as a
side effect of ordinary traffic — an absent branch is recreated the next time
someone opens the environment.

It is NOT adequate for the lease and sandbox parties, and that asymmetry is the
whole reason this module exists:

* a **stale lease** is only noticed by the request path when someone asks for
  that exact working copy. An abandoned sandbox's lease is precisely the one
  nobody asks for again, so it would occupy its Redis slot and its placement
  weight until the TTL — and be re-taken by a crash-looping holder repeatedly;
* an **orphan code location Pod** runs user code and holds a working-copy mount
  for as long as it lives. Nothing in the request path ever looks at it, because
  its environment is gone and no user will ever address it again;
* a **zombie task** leaves a submitter waiting on an outcome that will never be
  reported.

All three are resource leaks whose defining property is that no user action will
ever surface them. Hence a timer.

Design decisions worth pinning
------------------------------
**Read-only by default.** ``reconcile_sweep_repair`` starts False. The sweep
runs unattended over every workspace in the cell, and its repairs delete
Kubernetes objects and Redis keys; earning that trust needs a period of observing
what it WOULD have done, which the drift log provides. Turning it on is one
setting, and the detection value is available immediately either way.

**Cell-wide, one workspace at a time.** The parties are enumerated per cell, so
each workspace's pass re-reads them. That is deliberate: sharing one snapshot
across a long sweep would compare a lease read at the start against a registry
row read minutes later, which is exactly how ordinary hand-overs get reported as
drift. Correctness beats the round trips here — the sweep runs hourly, not per
request.

**One workspace's failure never stops the sweep.** A workspace whose Git remote
is unreachable must not prevent the next workspace's orphaned Pod from being
found.

**Mutual exclusion between sweeps.** Every control-service replica runs this
loop, and two of them reconciling one workspace concurrently is a real hazard:
both would see a lease as stale and the second would delete the successor the
first allowed to take over. The environment lock (Redis backed in multi-replica
deployments) provides the exclusion, keyed on a pseudo-environment so it cannot
collide with a real environment's lock; a replica that cannot take it skips that
workspace this tick.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass, field

from src.config import settings
from src.utils.errors import EnvironmentBusyError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Actor recorded for sweep-initiated reconciliation, matching the reaper's
# convention so the audit trail distinguishes scheduled work from a user's.
SWEEP_ACTOR = "system_reconcile_sweep"

# Drift kinds this loop exists for: the ones no user action would ever surface.
# Reported separately in the summary so the log line answers "did the sweep find
# anything only it could find?" without the reader cross-referencing the design.
SWEEP_EXCLUSIVE_KINDS = frozenset(
    {
        "stale_lease",
        "orphan_lease",
        "lease_split_brain",
        "orphan_codeloc_pod",
        "zombie_task",
        "stuck_operation",
    }
)


@dataclass
class SweepOutcome:
    """Result of one sweep, for logs, tests and the manual-trigger endpoint."""

    workspaces_scanned: int = 0
    workspaces_skipped_busy: int = 0
    workspaces_failed: int = 0
    drifts_found: int = 0
    drifts_repaired: int = 0
    critical_drifts: int = 0
    # kind -> count, across every workspace of this sweep.
    kinds: t.Dict[str, int] = field(default_factory=dict)
    # Operation resumer summary (see src.operations.resumer).
    operations: t.Dict[str, int] = field(default_factory=dict)

    @property
    def sweep_exclusive_drifts(self) -> int:
        """Drifts of the kinds only this loop can discover."""
        return sum(
            count
            for kind, count in self.kinds.items()
            if kind in SWEEP_EXCLUSIVE_KINDS
        )

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "workspaces_scanned": self.workspaces_scanned,
            "workspaces_skipped_busy": self.workspaces_skipped_busy,
            "workspaces_failed": self.workspaces_failed,
            "drifts_found": self.drifts_found,
            "drifts_repaired": self.drifts_repaired,
            "critical_drifts": self.critical_drifts,
            "sweep_exclusive_drifts": self.sweep_exclusive_drifts,
            "kinds": dict(sorted(self.kinds.items())),
            **self.operations,
        }


class ReconcileSweep:
    """Reconciles every workspace of the cell on a timer."""

    def __init__(
        self,
        *,
        workspaces: t.Optional[t.Any] = None,
        reconciler: t.Optional[t.Any] = None,
        lock_factory: t.Optional[t.Callable[..., t.Any]] = None,
        resumer: t.Optional[t.Any] = None,
    ) -> None:
        # Collaborators injected so the sweep's POLICY (what it skips, what it
        # tolerates, what it reports) is testable without Redis, a cluster or a
        # Git remote — the same discipline the reaper and the reconciler follow.
        self._workspaces = workspaces
        self._reconciler = reconciler
        self._lock_factory = lock_factory or _default_lock
        self._resumer = resumer

    async def run_once(self) -> SweepOutcome:
        """Resume stalled lifecycle operations, then reconcile every workspace.

        Operations first: a half-deleted workspace is still listed as active
        until its deletion finishes, and reconciling it would report (and with
        repair on, rebuild) resources the deletion is about to remove.
        """
        outcome = SweepOutcome()
        repair = bool(settings.reconcile_sweep_repair)
        await self._resume_operations(outcome, repair=repair)
        workspaces = await self._list_workspaces()

        for workspace in workspaces:
            try:
                report = await self._reconcile_one(workspace, repair=repair)
            except EnvironmentBusyError:
                # Another replica (or a user's own reconcile) holds this
                # workspace. Skipping is correct: two concurrent passes would
                # both call a lease stale and the second would delete the
                # successor the first permitted.
                outcome.workspaces_skipped_busy += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one workspace must not stop the sweep
                outcome.workspaces_failed += 1
                logger.error(
                    "reconcile_sweep_workspace_failed",
                    workspace_id=workspace.id,
                    error=str(exc),
                )
                continue

            outcome.workspaces_scanned += 1
            outcome.drifts_found += len(report.drifts)
            outcome.drifts_repaired += report.repaired_count
            outcome.critical_drifts += report.critical_count
            for drift in report.drifts:
                outcome.kinds[drift.kind] = outcome.kinds.get(drift.kind, 0) + 1

        _log_outcome(outcome, repair=repair, candidates=len(workspaces))
        return outcome

    async def start_periodic(self, interval_seconds: int) -> None:
        """Run :meth:`run_once` forever, one sweep per interval.

        Never propagates: a scheduled job that died on a transient database
        error would silently stop detecting leaks for the rest of the process's
        life — which is exactly the failure mode this loop exists to prevent.
        """
        interval = max(60, interval_seconds)
        logger.info(
            "reconcile_sweep_started",
            interval_seconds=interval,
            repair=bool(settings.reconcile_sweep_repair),
        )
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("reconcile_sweep_stopped")
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("reconcile_sweep_cycle_failed", error=str(exc))

    # ── internals ──────────────────────────────────────────────────────

    async def _resume_operations(self, outcome: SweepOutcome, *, repair: bool) -> None:
        """Drive interrupted create/delete operations to a terminal state.

        Same switch as the rest of the sweep: detection always, action only
        with ``reconcile_sweep_repair``. Never raises — a broken operations
        table must not stop lease and Pod leak detection.
        """
        resumer = self._resumer or _default_resumer()
        try:
            result = await resumer.run_once(repair=repair)
        except Exception as exc:  # noqa: BLE001 - keep the sweep going
            logger.error("reconcile_sweep_operation_resume_failed", error=str(exc))
            return
        outcome.operations = result.as_dict()
        if result.stuck:
            outcome.kinds["stuck_operation"] = result.stuck
            outcome.drifts_found += result.stuck
            outcome.drifts_repaired += result.resumed + result.superseded

    async def _list_workspaces(self) -> t.List[t.Any]:
        """Active workspaces to sweep, bounded by the configured maximum."""
        repository = self._workspaces or _default_workspace_repository()
        try:
            return await repository.list_active(
                limit=max(0, int(settings.reconcile_sweep_max_workspaces))
            )
        except Exception as exc:  # noqa: BLE001 - nothing to sweep, not a crash
            logger.error("reconcile_sweep_workspace_list_failed", error=str(exc))
            return []

    async def _reconcile_one(self, workspace: t.Any, *, repair: bool) -> t.Any:
        """Reconcile one workspace under the cross-replica lock.

        The lock is taken per WORKSPACE rather than for the whole sweep so that
        several replicas can make progress in parallel on different workspaces,
        while never colliding on one.
        """
        reconciler = self._reconciler or _default_reconciler()
        if not repair:
            # A read-only pass mutates nothing, so it needs no mutual exclusion:
            # two replicas reporting the same drift is noise in the log, not a
            # correctness problem.
            return await reconciler.reconcile_workspace(
                workspace=workspace, actor_user_id=SWEEP_ACTOR, repair=False
            )

        async with self._lock_factory(workspace.id):
            return await reconciler.reconcile_workspace(
                workspace=workspace, actor_user_id=SWEEP_ACTOR, repair=True
            )


def _log_outcome(outcome: SweepOutcome, *, repair: bool, candidates: int) -> None:
    """One structured line per sweep; louder when something needs a human."""
    payload = {
        "candidates": candidates,
        "repair": repair,
        **outcome.as_dict(),
    }
    if outcome.critical_drifts:
        # Split brain is the one kind that means the single-writer invariant is
        # currently violated. It must not hide inside an info line.
        logger.error("reconcile_sweep_completed_with_critical_drift", **payload)
    elif outcome.drifts_found or outcome.workspaces_failed:
        logger.warning("reconcile_sweep_completed_with_drift", **payload)
    else:
        logger.info("reconcile_sweep_completed", **payload)


def _default_workspace_repository() -> t.Any:
    """Resolve the workspace repository lazily (import graph)."""
    from src.workspace.repository import WorkspaceRepository

    return WorkspaceRepository()


def _default_reconciler() -> t.Any:
    """Resolve the reconciler lazily: it pulls in the whole binder stack."""
    from src.environments.reconciler import get_environment_reconciler

    return get_environment_reconciler()


def _default_resumer() -> t.Any:
    """Resolve the operation resumer lazily (it imports the workspace manager)."""
    from src.operations.resumer import get_operation_resumer

    return get_operation_resumer()


def _default_lock(workspace_id: str):
    """Cross-replica mutual exclusion for one workspace's repairing pass.

    Keyed on a pseudo-environment ``__sweep__``, which is NOT a valid environment
    name — so this lock can never collide with a real environment's lock. The
    consequence is deliberate and worth stating: the sweep serializes against
    OTHER SWEEPS (the actual hazard — two replicas both calling a lease stale,
    the second deleting the successor the first allowed), but not against a user's
    own reconcile of the same workspace.

    That is correct because the two cannot conflict on the lease party: a user's
    reconcile is environment-scoped and draws no lease or sandbox verdict at all
    (see ``reconcile_environment``), while the per-environment repairs the sweep
    performs each take the real environment lock inside the registry.
    """
    from src.core.env_locks import environment_lock_by_key

    return environment_lock_by_key(
        workspace_id, "__sweep__", operation="reconcile_sweep"
    )


_sweep: t.Optional[ReconcileSweep] = None


def get_reconcile_sweep() -> ReconcileSweep:
    """Get or create the global :class:`ReconcileSweep` (stateless)."""
    global _sweep
    if _sweep is None:
        _sweep = ReconcileSweep()
    return _sweep


__all__ = [
    "SWEEP_ACTOR",
    "SWEEP_EXCLUSIVE_KINDS",
    "ReconcileSweep",
    "SweepOutcome",
    "get_reconcile_sweep",
]
