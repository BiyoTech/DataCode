"""ReconcileSweep tests — the loop that makes lease/sandbox drift discoverable.

What matters here is the sweep's POLICY, not reconciliation itself (that is
pinned in ``test_environment_reconciler.py``):

* repair is OFF by default, so an operator observes before the loop deletes;
* a workspace held by another replica is skipped, never forced — two concurrent
  repairing passes would both call a lease stale and the second would delete the
  successor the first allowed;
* one workspace's failure never stops the sweep, because the next workspace's
  orphaned Pod still needs finding;
* the summary distinguishes the drift kinds only this loop can discover, which is
  what tells an operator whether the timer is earning its keep.
"""

from __future__ import annotations

import contextlib
import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from src.config import settings
from src.environments.reconciler import (
    SEVERITY_CRITICAL,
    Drift,
    ReconcileReport,
)
from src.environments.sweep import SWEEP_ACTOR, ReconcileSweep
from src.utils.errors import EnvironmentBusyError


@dataclass
class _Workspace:
    id: str
    tenant_id: str = "t1"


@dataclass
class _FakeWorkspaces:
    workspaces: t.List[_Workspace] = field(default_factory=list)
    limits: t.List[int] = field(default_factory=list)
    fail: bool = False

    async def list_active(self, limit: int = 0):
        if self.fail:
            raise RuntimeError("database unreachable")
        self.limits.append(limit)
        return list(self.workspaces if limit <= 0 else self.workspaces[:limit])


class _FakeReconciler:
    """Returns a scripted report per workspace, recording how it was called."""

    def __init__(
        self,
        reports: t.Optional[t.Dict[str, ReconcileReport]] = None,
        *,
        raises: t.Optional[t.Dict[str, Exception]] = None,
    ) -> None:
        self.reports = reports or {}
        self.raises = raises or {}
        self.calls: t.List[t.Tuple[str, str, bool]] = []

    async def reconcile_workspace(self, *, workspace, actor_user_id, repair=True):
        self.calls.append((workspace.id, actor_user_id, repair))
        if workspace.id in self.raises:
            raise self.raises[workspace.id]
        return self.reports.get(workspace.id) or _report(workspace.id)


def _report(workspace_id: str, *drifts: Drift) -> ReconcileReport:
    return ReconcileReport(
        workspace_id=workspace_id,
        scope="workspace",
        checked_at=datetime.now(timezone.utc),
        drifts=list(drifts),
    )


def _drift(kind: str, *, repaired: bool = False, severity: str = "warning") -> Drift:
    return Drift(
        kind=kind,
        environment="dev_alice",
        detail="",
        repairable=True,
        repaired=repaired,
        severity=severity,
    )


class _LockRecorder:
    """A lock that can be made unavailable, recording every attempt.

    Shaped like the production lock: an async context manager that raises INSIDE
    ``__aenter__``. A synchronous stand-in would pass while the sweep only
    handled the wrong failure shape.
    """

    def __init__(self, *, busy: t.Optional[t.Set[str]] = None) -> None:
        self.busy = busy or set()
        self.taken: t.List[str] = []

    def __call__(self, workspace_id: str):
        self.taken.append(workspace_id)
        busy = workspace_id in self.busy

        @contextlib.asynccontextmanager
        async def _lock():
            if busy:
                raise EnvironmentBusyError(workspace_id, "reconcile_sweep")
            yield

        return _lock()


@pytest.fixture()
def repair_on(monkeypatch):
    """Enable repairing sweeps for the tests that assert on repair behaviour."""
    monkeypatch.setattr(settings, "reconcile_sweep_repair", True)


def _sweep(workspaces, reconciler, lock=None, resumer=None) -> ReconcileSweep:
    return ReconcileSweep(
        workspaces=workspaces,
        reconciler=reconciler,
        lock_factory=lock or _LockRecorder(),
        resumer=resumer or _FakeResumer(),
    )


class _FakeResumer:
    """Records calls; optionally reports stuck operations or raises."""

    def __init__(self, outcome=None, error: t.Optional[Exception] = None, log=None):
        from src.operations.resumer import ResumeOutcome

        self.outcome = outcome or ResumeOutcome()
        self.error = error
        self.calls: list = []
        self.log = log

    async def run_once(self, *, repair: bool):
        self.calls.append(repair)
        if self.log is not None:
            self.log.append("resume")
        if self.error is not None:
            raise self.error
        return self.outcome


class TestOperationResume:
    async def test_operations_are_resumed_before_workspaces_are_reconciled(self):
        """A half-deleted workspace is still listed until its deletion finishes."""
        order: list = []

        class _OrderedReconciler(_FakeReconciler):
            async def reconcile_workspace(self, workspace, **kwargs):
                order.append("reconcile")
                return await _FakeReconciler.reconcile_workspace(self, workspace, **kwargs)

        resumer = _FakeResumer(log=order)
        sweep = _sweep(
            _FakeWorkspaces([_Workspace("ws-1")]), _OrderedReconciler(), resumer=resumer
        )

        await sweep.run_once()

        assert order[0] == "resume"
        assert resumer.calls == [settings.reconcile_sweep_repair]

    async def test_stuck_operations_are_reported_as_drift(self):
        from src.operations.resumer import ResumeOutcome

        resumer = _FakeResumer(ResumeOutcome(stuck=2, resumed=1, superseded=1))
        sweep = _sweep(_FakeWorkspaces([]), _FakeReconciler(), resumer=resumer)

        outcome = await sweep.run_once()

        assert outcome.kinds["stuck_operation"] == 2
        assert outcome.drifts_repaired == 2
        assert outcome.as_dict()["operations_stuck"] == 2

    async def test_a_broken_operations_table_does_not_stop_the_sweep(self):
        reconciler = _FakeReconciler()
        sweep = _sweep(
            _FakeWorkspaces([_Workspace("ws-1")]),
            reconciler,
            resumer=_FakeResumer(error=RuntimeError("table missing")),
        )

        await sweep.run_once()

        assert [call[0] for call in reconciler.calls] == ["ws-1"]


class TestRepairPolicy:
    async def test_repair_is_off_by_default(self):
        """Detection first: the loop must not delete before it is trusted."""
        assert settings.reconcile_sweep_repair is False

        reconciler = _FakeReconciler()
        sweep = _sweep(_FakeWorkspaces([_Workspace("ws-1")]), reconciler)

        await sweep.run_once()

        assert reconciler.calls == [("ws-1", SWEEP_ACTOR, False)]

    async def test_read_only_pass_takes_no_lock(self):
        """Nothing is mutated, so two replicas reporting the same drift is fine."""
        lock = _LockRecorder()
        sweep = _sweep(_FakeWorkspaces([_Workspace("ws-1")]), _FakeReconciler(), lock)

        await sweep.run_once()

        assert lock.taken == []

    async def test_repairing_pass_takes_the_lock(self, repair_on):
        lock = _LockRecorder()
        reconciler = _FakeReconciler()
        sweep = _sweep(_FakeWorkspaces([_Workspace("ws-1")]), reconciler, lock)

        await sweep.run_once()

        assert lock.taken == ["ws-1"]
        assert reconciler.calls == [("ws-1", SWEEP_ACTOR, True)]

    async def test_workspace_held_by_another_replica_is_skipped(self, repair_on):
        """Both passes would call a lease stale; the second would delete a successor."""
        lock = _LockRecorder(busy={"ws-1"})
        reconciler = _FakeReconciler()
        sweep = _sweep(
            _FakeWorkspaces([_Workspace("ws-1"), _Workspace("ws-2")]),
            reconciler,
            lock,
        )

        outcome = await sweep.run_once()

        assert outcome.workspaces_skipped_busy == 1
        assert outcome.workspaces_scanned == 1
        # The contended workspace was never reconciled; the other one was.
        assert [call[0] for call in reconciler.calls] == ["ws-2"]


class TestResilience:
    async def test_one_failing_workspace_does_not_stop_the_sweep(self):
        reconciler = _FakeReconciler(raises={"ws-1": RuntimeError("git unreachable")})
        sweep = _sweep(
            _FakeWorkspaces([_Workspace("ws-1"), _Workspace("ws-2")]), reconciler
        )

        outcome = await sweep.run_once()

        assert outcome.workspaces_failed == 1
        assert outcome.workspaces_scanned == 1
        assert [call[0] for call in reconciler.calls] == ["ws-1", "ws-2"]

    async def test_unreadable_workspace_list_yields_an_empty_sweep(self):
        reconciler = _FakeReconciler()
        sweep = _sweep(_FakeWorkspaces(fail=True), reconciler)

        outcome = await sweep.run_once()

        assert outcome.workspaces_scanned == 0
        assert reconciler.calls == []

    async def test_the_configured_bound_is_passed_to_the_query(self, monkeypatch):
        monkeypatch.setattr(settings, "reconcile_sweep_max_workspaces", 1)
        workspaces = _FakeWorkspaces([_Workspace("ws-1"), _Workspace("ws-2")])
        reconciler = _FakeReconciler()

        await _sweep(workspaces, reconciler).run_once()

        assert workspaces.limits == [1]
        assert [call[0] for call in reconciler.calls] == ["ws-1"]


class TestSummary:
    async def test_counts_are_aggregated_across_workspaces(self):
        reconciler = _FakeReconciler(
            {
                "ws-1": _report("ws-1", _drift("stale_lease", repaired=True)),
                "ws-2": _report(
                    "ws-2",
                    _drift("orphan_codeloc_pod", repaired=True),
                    _drift("commit_drift"),
                ),
            }
        )
        sweep = _sweep(
            _FakeWorkspaces([_Workspace("ws-1"), _Workspace("ws-2")]), reconciler
        )

        outcome = await sweep.run_once()

        assert outcome.drifts_found == 3
        assert outcome.drifts_repaired == 2
        assert outcome.kinds == {
            "stale_lease": 1,
            "orphan_codeloc_pod": 1,
            "commit_drift": 1,
        }

    async def test_sweep_exclusive_drifts_exclude_the_original_parties(self):
        """The number that answers "is the timer finding what only it can find?".

        ``commit_drift`` is surfaced by ordinary traffic too, so counting it here
        would make the sweep look valuable on a cell where it found nothing new.
        """
        reconciler = _FakeReconciler(
            {
                "ws-1": _report(
                    "ws-1",
                    _drift("commit_drift"),
                    _drift("orphan_branch"),
                    _drift("zombie_task", repaired=True),
                )
            }
        )
        sweep = _sweep(_FakeWorkspaces([_Workspace("ws-1")]), reconciler)

        outcome = await sweep.run_once()

        assert outcome.drifts_found == 3
        assert outcome.sweep_exclusive_drifts == 1

    async def test_critical_drift_is_counted(self):
        reconciler = _FakeReconciler(
            {
                "ws-1": _report(
                    "ws-1",
                    _drift("lease_split_brain", severity=SEVERITY_CRITICAL),
                )
            }
        )
        sweep = _sweep(_FakeWorkspaces([_Workspace("ws-1")]), reconciler)

        outcome = await sweep.run_once()

        assert outcome.critical_drifts == 1
        assert outcome.as_dict()["critical_drifts"] == 1
