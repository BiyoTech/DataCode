"""Drift detection and repair across the registry, Git, SQLMesh and Dagster.

The registry is the source of truth: when the real world disagrees with it,
THIS module is the only component allowed to repair the disagreement. Repairs
are re-invocations of the same idempotent operations the user-facing flows
use — never bespoke surgery — so a repair and a user action can never fight
over how a resource should look.

What is repaired vs. only reported
----------------------------------
Repaired automatically (idempotent, zero-data, or rebuilding derived state):

* ``missing_branch`` — the registry's own branch, re-created from the base it
  declares;
* ``missing_virtual_env`` — a pointer switch, zero data cost;
* ``missing_deployment`` — derived state, rebuilt from a working copy;
* ``orphan_sqlmesh_env`` — views nobody can promote from, invalidated.

Reported only (a human decides):

* ``commit_drift`` needs an apply (the user chooses when to pay for a
  backfill);
* ``orphan_branch`` may be someone's WIP — adopting it as an environment
  creates schemas and a deployment nobody asked for (design §11.2).

P5: the four "parties" are reached through the HTTP binders — Git and the
SQLMesh engine live inside workspace-runtime, deployments inside the runtime's
Dagster data plane. The reconciler itself stays free of filesystem access,
exactly like the rest of the control plane.

P8: two more parties, completing the design's five-way reconciliation (§05.5.1).

* the LEASE party (``src.environments.lease_binder``) is read straight from
  Redis, which the control plane already speaks for its environment locks;
* the SANDBOX party (``src.environments.sandbox_binder``) is read through the
  runtime, the only service holding a cluster client.

Their drift kinds follow the same repair/report split as the original four:
``stale_lease`` and ``orphan_lease`` are compare-and-swap deletes of a claim
whose owner is provably gone (safe to automate, and leaving them blocks the
working copy for a full TTL); ``orphan_codeloc_pod`` and ``zombie_task`` are
reclaims of derived state; ``lease_split_brain`` is REPORTED ONLY, because
choosing which of two claimants keeps a working copy is precisely the arbitration
the design assigns to the file lock, not to a periodic loop reading stale data.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone

from datacode_common import naming
from datacode_common.lease import LeaseRecord
from datacode_common.naming import LeaseKeyError, split_lease_key

from src.config import settings
from src.environments.deployment_binder import get_deployment_binder
from src.environments.git_binder import GitBinder
from src.environments.lease_binder import LeaseWorld, get_lease_binder
from src.environments.models import EnvironmentModel
from src.environments.repository import EnvironmentRepository
from src.environments.sandbox_binder import SandboxWorld, get_sandbox_binder
from src.environments.sqlmesh_binder import get_sqlmesh_binder
from src.records.repository import get_task_record_repository
from src.utils.logging import get_logger
from src.workspace.models import WorkspaceModel

logger = get_logger(__name__)

# Drift kinds, mirroring the classification table of the design document.
DRIFT_MISSING_BRANCH = "missing_branch"
DRIFT_MISSING_VIRTUAL_ENV = "missing_virtual_env"
DRIFT_ORPHAN_BRANCH = "orphan_branch"
DRIFT_ORPHAN_SQLMESH_ENV = "orphan_sqlmesh_env"
DRIFT_COMMIT_DRIFT = "commit_drift"
DRIFT_APPLIED_DIRTY = "applied_dirty"
DRIFT_STALE_RESOURCES = "stale_resources"
DRIFT_MISSING_DEPLOYMENT = "missing_deployment"
DRIFT_STALE_DEPLOYMENT = "stale_deployment"
# Lease and sandbox parties (design 05 §5.1, landed in P8).
DRIFT_ORPHAN_LEASE = "orphan_lease"
DRIFT_STALE_LEASE = "stale_lease"
DRIFT_LEASE_SPLIT_BRAIN = "lease_split_brain"
DRIFT_ORPHAN_CODELOC_POD = "orphan_codeloc_pod"
DRIFT_ZOMBIE_TASK = "zombie_task"

# Statuses whose row still claims live resources.
_LIVE_STATUSES = (
    naming.STATUS_ACTIVE,
    naming.STATUS_APPLYING,
    naming.STATUS_PROMOTING,
    naming.STATUS_DRIFTED,
    naming.STATUS_FAILED,
)

# Drift severities. ``critical`` is reserved for the invariant-violation kind
# (``lease_split_brain``): the design escalates it to a Page because two writers
# on one working copy is the one failure mode the lease exists to prevent.
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass
class Drift:
    """One detected divergence between the registry and the real world.

    ``repairable`` separates "the platform can fix this alone" from "a human
    must decide"; ``repaired`` reports the outcome when a repair was attempted.
    ``severity`` exists because one kind — ``lease_split_brain`` — is a
    correctness alarm rather than housekeeping: the design gives it a Page-level
    alert, and a field on the drift is what lets a log pipeline route it there
    without pattern-matching kind names.
    """

    kind: str
    environment: str
    detail: str
    repairable: bool = False
    repaired: bool = False
    repair_detail: str = ""
    severity: str = SEVERITY_INFO

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "kind": self.kind,
            "environment": self.environment,
            "detail": self.detail,
            "repairable": self.repairable,
            "repaired": self.repaired,
            "repair_detail": self.repair_detail,
            "severity": self.severity,
        }


@dataclass
class ReconcileReport:
    """Outcome of one reconciliation pass."""

    workspace_id: str
    scope: str
    checked_at: datetime
    # None means "could not read the engine state" — the reconciler must
    # withhold every SQLMesh verdict during an outage instead of treating it
    # as "no environments exist".
    sqlmesh_reachable: t.Optional[bool] = None
    dagster_reachable: t.Optional[bool] = None
    # Same contract for the two parties added in P8: None = no verdict drawn.
    lease_readable: t.Optional[bool] = None
    sandbox_readable: t.Optional[bool] = None
    environments_checked: int = 0
    drifts: t.List[Drift] = field(default_factory=list)

    @property
    def repaired_count(self) -> int:
        return sum(1 for drift in self.drifts if drift.repaired)

    @property
    def critical_count(self) -> int:
        """Drifts that need a human NOW rather than at the next pass."""
        return sum(1 for drift in self.drifts if drift.severity == SEVERITY_CRITICAL)

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "workspace_id": self.workspace_id,
            "scope": self.scope,
            "checked_at": self.checked_at.isoformat(),
            "sqlmesh_reachable": self.sqlmesh_reachable,
            "dagster_reachable": self.dagster_reachable,
            "lease_readable": self.lease_readable,
            "sandbox_readable": self.sandbox_readable,
            "environments_checked": self.environments_checked,
            "drift_count": len(self.drifts),
            "repaired_count": self.repaired_count,
            "critical_count": self.critical_count,
            "drifts": [drift.as_dict() for drift in self.drifts],
        }


class EnvironmentReconciler:
    """Finds and (when allowed) repairs divergence for one workspace."""

    def __init__(
        self,
        base_path: str = "",
        *,
        repository: t.Optional[EnvironmentRepository] = None,
        git_binder: t.Optional[GitBinder] = None,
        deployment_binder: t.Optional[t.Any] = None,
        sqlmesh_binder: t.Optional[t.Any] = None,
        lease_binder: t.Optional[t.Any] = None,
        sandbox_binder: t.Optional[t.Any] = None,
        task_records: t.Optional[t.Any] = None,
        registry_factory: t.Optional[t.Callable[[], t.Any]] = None,
    ) -> None:
        self._repo = repository or EnvironmentRepository()
        self._git = git_binder or GitBinder(base_path)
        self._deployment = deployment_binder or get_deployment_binder()
        self._sqlmesh = sqlmesh_binder or get_sqlmesh_binder()
        self._leases = lease_binder or get_lease_binder()
        self._sandboxes = sandbox_binder or get_sandbox_binder()
        self._tasks = task_records or get_task_record_repository()
        self._registry_factory = registry_factory or _default_registry

    # ---- Public API ----

    async def reconcile_workspace(
        self,
        *,
        workspace: WorkspaceModel,
        actor_user_id: str,
        repair: bool = True,
    ) -> ReconcileReport:
        """Reconcile every environment of a workspace, plus its orphans.

        Orphans (a branch or a virtual environment nobody registered) can only be
        found at workspace scope: by definition they have no registry row to
        reconcile from.
        """
        models = await self._repo.list_by_workspace(
            workspace.id, include_archived=True
        )
        members = await self._git.list_member_user_ids(
            tenant_id=workspace.tenant_id, workspace_id=workspace.id
        )
        state = await self._read_state_environments(
            workspace=workspace, models=models, members=members
        )
        served = await self._read_served_deployments(workspace=workspace)
        # The lease and sandbox parties are read ONCE per pass and shared by
        # every check below: reading them per environment would compare a lease
        # observed now against a heartbeat observed a minute ago and invent
        # split brains out of ordinary hand-overs.
        leases = await self._leases.read_world()
        sandboxes = await self._sandboxes.read_world()

        report = ReconcileReport(
            workspace_id=workspace.id,
            scope="workspace",
            checked_at=datetime.now(timezone.utc),
            sqlmesh_reachable=state is not None,
            dagster_reachable=served is not None,
            lease_readable=leases is not None,
            sandbox_readable=_sandbox_readable(sandboxes),
        )

        live_models = [model for model in models if model.status in _LIVE_STATUSES]
        for model in live_models:
            await self._check_live_environment(
                model=model,
                workspace=workspace,
                members=members,
                state=state,
                served=served,
                repair=repair,
                report=report,
            )
        for model in models:
            if model.status == naming.STATUS_ARCHIVED:
                await self._check_archived_environment(
                    model=model,
                    workspace=workspace,
                    members=members,
                    state=state,
                    served=served,
                    repair=repair,
                    report=report,
                )
        report.environments_checked = len(models)

        await self._detect_orphans(
            workspace=workspace,
            models=models,
            members=members,
            state=state,
            repair=repair,
            report=report,
            actor_user_id=actor_user_id,
        )

        # Lease and sandbox parties. Workspace scope only: both are enumerated
        # cell-wide, and deciding "no registry row accounts for this" requires
        # the whole workspace's rows — an environment-scoped pass sees one row
        # and would call every sibling's lease an orphan.
        await self._check_leases(
            workspace=workspace,
            models=models,
            leases=leases,
            repair=repair,
            report=report,
        )
        await self._check_sandboxes(
            workspace=workspace,
            models=models,
            sandboxes=sandboxes,
            repair=repair,
            report=report,
        )

        logger.info(
            "environment_reconcile_completed",
            workspace_id=workspace.id,
            scope="workspace",
            environments_checked=report.environments_checked,
            drift_count=len(report.drifts),
            repaired_count=report.repaired_count,
            critical_count=report.critical_count,
            sqlmesh_reachable=report.sqlmesh_reachable,
            dagster_reachable=report.dagster_reachable,
            lease_readable=report.lease_readable,
            sandbox_readable=report.sandbox_readable,
        )
        return report

    async def reconcile_environment(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        actor_user_id: str,
        repair: bool = True,
    ) -> ReconcileReport:
        """Reconcile one environment against Git, the engine and the deployments.

        Orphan detection is out of scope here, and so are the lease and sandbox
        parties: deciding that a lease or a Pod is orphaned requires the whole
        workspace's rows, and a single-row pass would call every sibling
        environment's lease an orphan. ``lease_readable`` / ``sandbox_readable``
        therefore stay ``None`` — "no verdict drawn", which is exactly true.
        """
        members = await self._git.list_member_user_ids(
            tenant_id=workspace.tenant_id, workspace_id=workspace.id
        )
        state = await self._read_state_environments(
            workspace=workspace, models=[model], members=members
        )
        served = await self._read_served_deployments(workspace=workspace)

        report = ReconcileReport(
            workspace_id=workspace.id,
            scope=f"environment:{model.name}",
            checked_at=datetime.now(timezone.utc),
            sqlmesh_reachable=state is not None,
            dagster_reachable=served is not None,
            environments_checked=1,
        )

        if model.status == naming.STATUS_ARCHIVED:
            await self._check_archived_environment(
                model=model,
                workspace=workspace,
                members=members,
                state=state,
                served=served,
                repair=repair,
                report=report,
            )
        elif model.status in _LIVE_STATUSES:
            await self._check_live_environment(
                model=model,
                workspace=workspace,
                members=members,
                state=state,
                served=served,
                repair=repair,
                report=report,
            )

        if repair:
            await self._settle_status(model=model, report=report)

        await self._record_reconcile_event(
            model=model, actor_user_id=actor_user_id, report=report
        )
        logger.info(
            "environment_reconcile_completed",
            workspace_id=workspace.id,
            environment=model.name,
            scope="environment",
            drift_count=len(report.drifts),
            repaired_count=report.repaired_count,
            sqlmesh_reachable=report.sqlmesh_reachable,
            dagster_reachable=report.dagster_reachable,
        )
        return report

    # ---- Checks ----

    async def _check_live_environment(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        state: t.Optional[t.Set[str]],
        served: t.Optional[t.Set[str]],
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Verify that a live environment is backed by all of its resources."""
        repo = await self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
        local, remote = (
            (False, False)
            if not repo
            else await self._git.branch_presence(repo_path=repo, branch=model.branch)
        )
        # No clone at all on any replica says nothing about the remote, so the
        # branch cannot be declared missing.
        if repo and not local and not remote:
            report.drifts.append(
                await self._repair_missing_branch(
                    model=model, workspace=workspace, members=members, repair=repair
                )
            )
        elif repo and not remote and workspace.git_repo_url:
            # Local-only: other members cannot materialize the environment.
            report.drifts.append(
                await self._repair_unpublished_branch(
                    model=model, workspace=workspace, members=members, repair=repair
                )
            )

        if state is not None and model.name not in state:
            report.drifts.append(
                await self._repair_missing_virtual_env(
                    model=model, workspace=workspace, members=members, repair=repair
                )
            )

        # ``served is None`` means the runtime could not be read: no
        # verdict follows, exactly like an unreachable warehouse above.
        if served is not None and _expected_deployment_name(model) not in served:
            report.drifts.append(
                await self._repair_missing_deployment(
                    model=model, workspace=workspace, members=members, repair=repair
                )
            )

        await self._check_code_data_sync(model=model, members=members, report=report)

    async def _check_code_data_sync(
        self,
        *,
        model: EnvironmentModel,
        members: t.Sequence[str],
        report: ReconcileReport,
    ) -> None:
        """Report whether the data still corresponds to the branch's code.

        Never repaired: bringing the data back in sync is an apply, whose cost
        (a backfill) only the user can authorize.
        """
        if model.last_applied_dirty:
            report.drifts.append(
                Drift(
                    kind=DRIFT_APPLIED_DIRTY,
                    environment=model.name,
                    detail=(
                        "the current data was applied from uncommitted code, so "
                        "no commit describes it; commit and apply again"
                    ),
                )
            )
            return

        if not model.last_applied_commit:
            return

        head = await self._git.worktree_head(
            tenant_id=model.tenant_id,
            workspace_id=model.workspace_id,
            environment=model.name,
        )
        if not head:
            return

        if head != model.last_applied_commit:
            report.drifts.append(
                Drift(
                    kind=DRIFT_COMMIT_DRIFT,
                    environment=model.name,
                    detail=(
                        f"branch is at {head[:8]} but the data was produced by "
                        f"{model.last_applied_commit[:8]}; re-apply to sync"
                    ),
                )
            )

    async def _check_archived_environment(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        state: t.Optional[t.Set[str]],
        served: t.Optional[t.Set[str]],
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Finish a destroy that could not release everything at the time.

        This is the counterpart of ``destroy``'s honest partial reporting: an
        archived row whose branch, virtual environment or deployment is still
        alive is exactly the state destroy leaves behind when the remote was
        unreachable, when nobody held a working copy, or when the runtime
        could not be reached. One ``release_archived_resources`` call finishes
        all of it; each drift kind then reports its own slice of the outcome.
        """
        repo = await self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
        local, remote = (
            (False, False)
            if not repo
            else await self._git.branch_presence(repo_path=repo, branch=model.branch)
        )
        worktree = await self._git.first_existing_worktree(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            environment=model.name,
            members=members,
        )
        virtual_env_alive = state is not None and model.name in state
        deployment_alive = (
            served is not None and _expected_deployment_name(model) in served
        )

        held = [
            name
            for name, alive in (
                ("local_branch", local),
                ("remote_branch", remote),
                ("working_copy", bool(worktree)),
                ("virtual_environment", virtual_env_alive),
            )
            if alive
        ]
        if not held and not deployment_alive:
            return

        released: t.Any = None
        release_error = ""
        if repair:
            try:
                released = await self._registry_factory().release_archived_resources(
                    environment_id=model.id,
                    tenant_id=model.tenant_id,
                    actor_user_id=_RECONCILER_ACTOR,
                )
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                release_error = str(exc)

        if held:
            detail = "archived environment still holds: " + ", ".join(held)
            repaired, repair_detail = False, release_error
            if released is not None:
                repaired = (
                    released.worktrees_removed > 0
                    or released.remote_branch_deleted
                    or released.virtual_environment_released
                )
                repair_detail = (
                    f"worktrees_removed={released.worktrees_removed}, "
                    f"remote_branch_deleted={released.remote_branch_deleted}, "
                    f"virtual_environment_released="
                    f"{released.virtual_environment_released}"
                )
            report.drifts.append(
                Drift(
                    kind=DRIFT_STALE_RESOURCES,
                    environment=model.name,
                    detail=detail,
                    repairable=True,
                    repaired=repaired,
                    repair_detail=repair_detail,
                )
            )

        if deployment_alive:
            detail = (
                "archived environment's deployment is still served: "
                f"{_expected_deployment_name(model)}"
            )
            repaired, repair_detail = False, release_error
            if released is not None:
                repaired = released.deployment_destroyed
                repair_detail = (
                    "deployment destroyed"
                    if repaired
                    else "the runtime refused the destroy"
                )
            report.drifts.append(
                Drift(
                    kind=DRIFT_STALE_DEPLOYMENT,
                    environment=model.name,
                    detail=detail,
                    repairable=True,
                    repaired=repaired,
                    repair_detail=repair_detail,
                )
            )

    async def _detect_orphans(
        self,
        *,
        workspace: WorkspaceModel,
        models: t.Sequence[EnvironmentModel],
        members: t.Sequence[str],
        state: t.Optional[t.Set[str]],
        repair: bool,
        report: ReconcileReport,
        actor_user_id: str,
    ) -> None:
        """Find branches and virtual environments no registry row accounts for."""
        registered = {model.name for model in models if model.status != naming.STATUS_ARCHIVED}

        repo = await self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
        if repo:
            for environment in sorted(
                await self._git.list_environment_branches(repo_path=repo)
            ):
                if environment in registered:
                    continue
                # Reported only: whether an unregistered branch should become an
                # environment is a human decision (design §11.2), and adopting it
                # automatically would create schemas and a deployment nobody asked
                # for.
                report.drifts.append(
                    Drift(
                        kind=DRIFT_ORPHAN_BRANCH,
                        environment=environment,
                        detail=(
                            f"branch {naming.branch_of(environment)} exists but no "
                            "environment is registered for it; adopt it to use it"
                        ),
                    )
                )

        if state is None:
            return
        for environment in sorted(state - registered):
            if environment == naming.PRODUCTION_ENV:
                # SQLMesh always owns a `prod` environment; the workspace may
                # simply name its production environment differently.
                continue
            await self._repair_orphan_virtual_env(
                workspace=workspace,
                environment=environment,
                members=members,
                models=models,
                repair=repair,
                report=report,
            )

    # ---- Lease party (design 05 §5.1) ----

    async def _check_leases(
        self,
        *,
        workspace: WorkspaceModel,
        models: t.Sequence[EnvironmentModel],
        leases: t.Optional[LeaseWorld],
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Compare this workspace's leases against the registry and the heartbeats.

        Three verdicts, in the order they must be evaluated:

        1. **split brain** — two runtimes claim one working copy. Checked FIRST
           and never repaired: while two claimants exist, "the holder is gone" is
           false by definition, so evaluating staleness first could delete the
           claim of a live writer.
        2. **orphan** — no live registry row accounts for the lease, so nothing
           will ever release it.
        3. **stale** — the claiming runtime has no heartbeat. Its process is
           gone, and without this the claim blocks the working copy for a full
           TTL; a lease nobody requests again is never noticed by the request
           path at all.

        A truncated pass repairs nothing (it may not conclude "no owner" about
        keys it never reached) but still reports what it did see.
        """
        if leases is None:
            return

        # Rows that legitimately hold a lease. Archived rows are excluded on
        # purpose: an archived environment's lease protects nothing, which is
        # precisely the orphan below.
        live_environments = {
            model.name for model in models if model.status in _LIVE_STATUSES
        }
        live_runtimes = leases.live_runtime_ids()
        may_repair = repair and not leases.truncated

        for lease_key, record in sorted(leases.leases.items()):
            owner = _lease_owner(lease_key)
            if owner is None:
                continue
            tenant_id, workspace_id, _user_id, environment = owner
            if workspace_id != workspace.id or tenant_id != workspace.tenant_id:
                # Every workspace's pass sees the whole cell's leases; scoping
                # here is what keeps one workspace from judging another's.
                continue

            claimants = leases.claimants_of(lease_key)
            if len(claimants) > 1:
                report.drifts.append(
                    Drift(
                        kind=DRIFT_LEASE_SPLIT_BRAIN,
                        environment=environment,
                        detail=(
                            f"lease {lease_key} is claimed by {len(claimants)} "
                            f"runtimes ({', '.join(claimants)}); the working "
                            "copy's file lock is the arbiter, and the losing "
                            "holder must release it"
                        ),
                        severity=SEVERITY_CRITICAL,
                    )
                )
                continue

            if environment not in live_environments:
                report.drifts.append(
                    await self._repair_lease_claim(
                        kind=DRIFT_ORPHAN_LEASE,
                        record=record,
                        environment=environment,
                        detail=(
                            f"lease {lease_key} is held by {record.runtime_id} "
                            "but no live environment is registered for it; "
                            "nothing will release it"
                        ),
                        repair=may_repair,
                    )
                )
                continue

            if record.runtime_id not in live_runtimes:
                report.drifts.append(
                    await self._repair_lease_claim(
                        kind=DRIFT_STALE_LEASE,
                        record=record,
                        environment=environment,
                        detail=(
                            f"lease {lease_key} is held by {record.runtime_id}, "
                            "whose heartbeat is gone; the working copy stays "
                            "unreachable until the lease TTL lapses"
                        ),
                        repair=may_repair,
                    )
                )

    async def _repair_lease_claim(
        self,
        *,
        kind: str,
        record: LeaseRecord,
        environment: str,
        detail: str,
        repair: bool,
    ) -> Drift:
        """Release one dead lease claim (compare-and-swap on its token).

        Shared by both releasable lease kinds: the DETECTION differs (no
        environment vs. no heartbeat) but the repair is identical, and a second
        copy of it would be a second chance for one to become a blind delete.
        """
        released = await self._leases.release_stale(record) if repair else False
        if not repair:
            repair_detail = ""
        elif released:
            repair_detail = "released"
        else:
            repair_detail = "the lease was re-acquired or had already expired"
        return Drift(
            kind=kind,
            environment=environment,
            detail=detail,
            repairable=True,
            repaired=released,
            repair_detail=repair_detail,
            severity=SEVERITY_WARNING,
        )

    # ---- Sandbox party (design 05 §5.1) ----

    async def _check_sandboxes(
        self,
        *,
        workspace: WorkspaceModel,
        models: t.Sequence[EnvironmentModel],
        sandboxes: t.Optional[SandboxWorld],
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Compare running user-code Pods and unfinished tasks with the registry."""
        if sandboxes is None or not sandboxes.available:
            return
        await self._check_code_location_pods(
            models=models, sandboxes=sandboxes, repair=repair, report=report
        )
        await self._check_zombie_tasks(
            workspace=workspace, sandboxes=sandboxes, repair=repair, report=report
        )

    async def _check_code_location_pods(
        self,
        *,
        models: t.Sequence[EnvironmentModel],
        sandboxes: SandboxWorld,
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Delete code-location Pods of environments that no longer exist.

        A code-location Pod runs the user's Python for as long as it lives, so an
        orphan is not merely idle capacity: it keeps a working copy mounted and
        holds a slot on the user-code node pool. It is derived state (the manager
        recreates it on demand), which is what makes removing it safe to
        automate — the same reasoning as ``orphan_sqlmesh_env``.

        Only environments THIS workspace owns are judged: the namespace is
        cell-wide, and another workspace's Pod belongs to that workspace's pass.
        """
        grouped = sandboxes.code_location_pods_by_environment()
        if grouped is None:
            return

        owned = {model.name: model for model in models}
        live = {model.name for model in models if model.status in _LIVE_STATUSES}

        for environment, pods in sorted(grouped.items()):
            if environment not in owned or environment in live:
                continue
            for pod in pods:
                pod_name = str(pod.get("pod_name", ""))
                if not pod_name:
                    continue
                deleted = (
                    await self._sandboxes.delete_code_location_pod(
                        pod_name=pod_name, environment=environment
                    )
                    if repair
                    else False
                )
                if not repair:
                    repair_detail = ""
                elif deleted:
                    repair_detail = "pod and service deleted"
                else:
                    repair_detail = "the runtime refused or could not reach the Pod"
                report.drifts.append(
                    Drift(
                        kind=DRIFT_ORPHAN_CODELOC_POD,
                        environment=environment,
                        detail=(
                            f"code location Pod {pod_name} serves environment "
                            f"{environment}, whose registry row is "
                            f"{owned[environment].status}"
                        ),
                        repairable=True,
                        repaired=deleted,
                        repair_detail=repair_detail,
                        severity=SEVERITY_WARNING,
                    )
                )

    async def _check_zombie_tasks(
        self,
        *,
        workspace: WorkspaceModel,
        sandboxes: SandboxWorld,
        repair: bool,
        report: ReconcileReport,
    ) -> None:
        """Settle tasks whose row claims running while no Pod exists.

        The audit row is written BEFORE the vehicle (which is what makes a
        crashed submission visible), so a young task legitimately has no Pod yet;
        only rows older than the grace period are judged. Settling them is what
        turns "this plan is still running" — forever — into a failure a user can
        act on, and it is the row's own state machine being completed rather than
        an outside verdict, so it is safe to automate.
        """
        running = sandboxes.running_task_ids
        if running is None:
            return

        grace = max(0, int(settings.zombie_task_grace_seconds))
        unfinished = await self._tasks.list_unfinished(
            workspace_id=workspace.id, older_than_seconds=grace
        )
        for task in unfinished:
            if task.task_id in running:
                continue
            reason = (
                f"no sandbox Pod exists for this task; it was last seen "
                f"{task.status} on {task.runtime_id or 'an unknown runtime'} "
                f"{int(task.age_seconds())}s ago"
            )
            settled = (
                await self._tasks.mark_failed(task.task_id, reason) if repair else False
            )
            if not repair:
                repair_detail = ""
            elif settled:
                repair_detail = "marked failed"
            else:
                repair_detail = "the task reported its own outcome meanwhile"
            report.drifts.append(
                Drift(
                    kind=DRIFT_ZOMBIE_TASK,
                    environment=task.environment,
                    detail=(
                        f"task {task.task_id} ({task.kind}) is recorded as "
                        f"{task.status} but has no Pod"
                    ),
                    repairable=True,
                    repaired=settled,
                    repair_detail=repair_detail,
                    severity=SEVERITY_WARNING,
                )
            )

    # ---- Repairs ----

    async def _repair_missing_branch(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        repair: bool,
    ) -> Drift:
        """Re-create a branch the registry owns, from the base it declares."""
        detail = (
            f"branch {model.branch} exists neither locally nor on the remote, "
            f"but the environment is {model.status}"
        )
        if not repair:
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
            )

        base = await self._repo.get_by_name(workspace.id, model.base_env)
        if base is None:
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
                repair_detail=f"base environment {model.base_env} is gone too",
            )

        member = members[0] if members else model.owner_user_id
        try:
            materialization = await self._git.ensure_worktree(
                tenant_id=workspace.tenant_id,
                workspace_id=workspace.id,
                user_id=member,
                environment=model.name,
                branch=model.branch,
                start_branch=base.branch,
            )
            published = True
            if workspace.git_repo_url:
                result = await self._git.push_branch(
                    worktree_path=materialization.workspace_path,
                    branch=model.branch,
                    user_id=member,
                    git_repo_url=workspace.git_repo_url,
                )
                published = result.ok
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
                repaired=True,
                repair_detail=(
                    f"recreated from {base.branch} at "
                    f"{materialization.commit[:8]}, published={published}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
                repair_detail=str(exc),
            )

    async def _repair_unpublished_branch(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        repair: bool,
    ) -> Drift:
        """Push a branch that only ever existed in one member's clone."""
        detail = (
            f"branch {model.branch} exists locally but not on the remote, so "
            "other members cannot materialize this environment"
        )
        if not repair:
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
            )

        worktree = await self._git.first_existing_worktree(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            environment=model.name,
            members=members,
        )
        if not worktree:
            return Drift(
                kind=DRIFT_MISSING_BRANCH,
                environment=model.name,
                detail=detail,
                repairable=True,
                repair_detail="no working copy available to push from",
            )

        member = _owner_of_worktree(worktree, members)
        result = await self._git.push_branch(
            worktree_path=worktree,
            branch=model.branch,
            user_id=member,
            git_repo_url=workspace.git_repo_url,
        )
        return Drift(
            kind=DRIFT_MISSING_BRANCH,
            environment=model.name,
            detail=detail,
            repairable=True,
            repaired=result.ok,
            repair_detail="published" if result.ok else str(result.reason),
        )

    async def _repair_missing_virtual_env(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        repair: bool,
    ) -> Drift:
        """Re-create a virtual environment: a pointer switch, so zero data cost."""
        detail = (
            f"no SQLMesh environment named {model.name} exists in the "
            "workspace state"
        )
        if not repair:
            return Drift(
                kind=DRIFT_MISSING_VIRTUAL_ENV,
                environment=model.name,
                detail=detail,
                repairable=True,
            )

        worktree = await self._git.first_existing_worktree(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            environment=model.name,
            members=members,
        )
        if not worktree:
            return Drift(
                kind=DRIFT_MISSING_VIRTUAL_ENV,
                environment=model.name,
                detail=detail,
                repairable=True,
                repair_detail="no working copy available to reach the engine",
            )

        created = await self._sqlmesh.ensure_virtual_environment(
            workspace_path=worktree,
            environment=model.name,
            base_env=model.base_env,
        )
        return Drift(
            kind=DRIFT_MISSING_VIRTUAL_ENV,
            environment=model.name,
            detail=detail,
            repairable=True,
            repaired=created,
            repair_detail=(
                f"recreated from {model.base_env}" if created else "engine refused"
            ),
        )

    async def _repair_missing_deployment(
        self,
        *,
        model: EnvironmentModel,
        workspace: WorkspaceModel,
        members: t.Sequence[str],
        repair: bool,
    ) -> Drift:
        """Re-ensure a deployment the registry owns: a code location respawn.

        The Dagster-side registry is in-process state, so the most routine
        cause is simply a runtime restart; the repair is exactly what the UI's
        lazy ensure does, only triggered by the platform instead of a page
        view, and it restores the row's POLICY (``env_type`` /
        ``schedules_enabled``) alongside the process.
        """
        name = _expected_deployment_name(model)
        detail = (
            f"environment is {model.status} but the runtime serves no "
            f"deployment named {name}"
        )
        if not repair:
            return Drift(
                kind=DRIFT_MISSING_DEPLOYMENT,
                environment=model.name,
                detail=detail,
                repairable=True,
            )

        # Any member's working copy yields the same deployment: the name is a
        # pure function of workspace + environment, never of the user.
        worktree = await self._git.first_existing_worktree(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            environment=model.name,
            members=members,
        )
        if not worktree:
            return Drift(
                kind=DRIFT_MISSING_DEPLOYMENT,
                environment=model.name,
                detail=detail,
                repairable=True,
                repair_detail="no working copy available to load code from",
            )

        ensured = await self._deployment.ensure_deployment(
            workspace_path=worktree,
            environment=model.name,
            env_type=model.env_type,
            schedules_enabled=bool(model.schedules_enabled),
        )
        return Drift(
            kind=DRIFT_MISSING_DEPLOYMENT,
            environment=model.name,
            detail=detail,
            repairable=True,
            repaired=ensured,
            repair_detail=(
                f"ensured from a working copy (env_type={model.env_type}, "
                f"schedules_enabled={bool(model.schedules_enabled)})"
                if ensured
                else "the runtime refused the ensure"
            ),
        )

    async def _repair_orphan_virtual_env(
        self,
        *,
        workspace: WorkspaceModel,
        environment: str,
        members: t.Sequence[str],
        models: t.Sequence[EnvironmentModel],
        repair: bool,
        report: ReconcileReport,
        ) -> None:
        """Invalidate a virtual environment no live registry row accounts for.

        Safe to automate, unlike adopting an orphan branch: the views are
        derived state that any registered environment can rebuild for free, and
        leaving them means the engine's environment list keeps showing
        environments the platform does not manage.
        """
        detail = (
            f"the SQLMesh state holds environment {environment}, which no live "
            "registry row accounts for"
        )
        if not repair:
            report.drifts.append(
                Drift(
                    kind=DRIFT_ORPHAN_SQLMESH_ENV,
                    environment=environment,
                    detail=detail,
                    repairable=True,
                )
            )
            return

        # Any working copy of this workspace can reach the shared state; the
        # orphan itself usually has none.
        worktree = ""
        for model in models:
            worktree = await self._git.first_existing_worktree(
                tenant_id=workspace.tenant_id,
                workspace_id=workspace.id,
                environment=model.name,
                members=members,
            )
            if worktree:
                break

        if not worktree:
            report.drifts.append(
                Drift(
                    kind=DRIFT_ORPHAN_SQLMESH_ENV,
                    environment=environment,
                    detail=detail,
                    repairable=True,
                    repair_detail="no working copy available to reach the engine",
                )
            )
            return

        released = await self._sqlmesh.invalidate_virtual_environment(
            workspace_path=worktree, environment=environment
        )
        report.drifts.append(
            Drift(
                kind=DRIFT_ORPHAN_SQLMESH_ENV,
                environment=environment,
                detail=detail,
                repairable=True,
                repaired=released,
                repair_detail="invalidated" if released else "engine refused",
            )
        )

    async def _settle_status(
        self, *, model: EnvironmentModel, report: ReconcileReport
    ) -> None:
        """Complete the ``drifted -> active`` transition of the state machine.

        An environment marked ``drifted`` stays unusable-looking until something
        confirms it is healthy again. Only unrepaired, repairable drift keeps it
        there: ``commit_drift`` is a normal state of a branch under development,
        not a broken environment.
        """
        if model.status != naming.STATUS_DRIFTED:
            return
        if not report.sqlmesh_reachable:
            return
        unresolved = [
            drift
            for drift in report.drifts
            if drift.repairable and not drift.repaired
        ]
        if unresolved:
            return

        if await self._repo.update_status(
            model.id, naming.STATUS_ACTIVE, expected=naming.STATUS_DRIFTED
        ):
            logger.info(
                "environment_drift_resolved",
                environment_id=model.id,
                workspace_id=model.workspace_id,
            )

    # ---- Internals ----

    async def _read_state_environments(
        self,
        *,
        workspace: WorkspaceModel,
        models: t.Sequence[EnvironmentModel],
        members: t.Sequence[str],
    ) -> t.Optional[t.Set[str]]:
        """Read the engine's environment list through any available working copy."""
        for model in models:
            worktree = await self._git.first_existing_worktree(
                tenant_id=workspace.tenant_id,
                workspace_id=workspace.id,
                environment=model.name,
                members=members,
            )
            if not worktree:
                continue
            state = await self._sqlmesh.list_state_environments(
                workspace_path=worktree
            )
            if state is not None:
                return state
        return None

    async def _read_served_deployments(
        self, *, workspace: WorkspaceModel
    ) -> t.Optional[t.Set[str]]:
        """Names of the deployments the runtime serves FOR THIS WORKSPACE.

        One call per pass, shared by every environment check. The served list
        spans every workspace the runtime has loaded, so filtering by
        ``workspace_id`` is what keeps a stale verdict from destroying another
        workspace's deployment. ``None`` means unreachable: no verdict follows.
        """
        deployments = await self._deployment.list_deployments()
        if deployments is None:
            logger.warning(
                "served_deployments_unreachable",
                workspace_id=workspace.id,
            )
            return None
        return {
            item["deployment_name"]
            for item in deployments
            if item.get("workspace_id") == workspace.id
            and item.get("deployment_name")
        }

    async def _record_reconcile_event(
        self,
        *,
        model: EnvironmentModel,
        actor_user_id: str,
        report: ReconcileReport,
    ) -> None:
        """Append the reconciliation to the environment's timeline (best-effort)."""
        try:
            await self._repo.record_event(
                model.id,
                naming.ACTION_RECONCILE,
                actor_user_id=actor_user_id,
                succeeded=True,
                detail={
                    "drift_count": len(report.drifts),
                    "repaired_count": report.repaired_count,
                    "sqlmesh_reachable": report.sqlmesh_reachable,
                    "dagster_reachable": report.dagster_reachable,
                    "kinds": sorted({drift.kind for drift in report.drifts}),
                },
            )
        except Exception as exc:  # noqa: BLE001 - auditing must not fail the op
            logger.warning(
                "environment_reconcile_event_failed",
                environment_id=model.id,
                error=str(exc),
            )


# Actor recorded when the reconciler releases resources on nobody's behalf.
_RECONCILER_ACTOR = "system_reconciler"


def _default_registry() -> t.Any:
    """Resolve the registry lazily, breaking the import cycle with it."""
    from src.environments.registry import get_environment_registry

    return get_environment_registry()


def _expected_deployment_name(model: EnvironmentModel) -> str:
    """Deployment name of a row, recomputed when frozen empty (legacy rows).

    ``deployment_name`` is frozen at creation time so the two services can
    never disagree; rows created before the column existed simply get the pure
    function's value, which is what creation would have frozen anyway.
    """
    return model.deployment_name or naming.deployment_name(
        model.workspace_id, model.name
    )


def _sandbox_readable(world: t.Optional[SandboxWorld]) -> t.Optional[bool]:
    """Whether the sandbox party yielded usable data, for the report.

    ``None`` covers both "the runtime could not be reached" and "this deployment
    has no container plane": neither produces a verdict, and the report's job is
    to say that no verdict was drawn rather than to explain why. The reason is in
    the binder's log line.
    """
    if world is None or not world.available:
        return None
    return world.code_location_pods is not None or world.task_pods is not None


def _lease_owner(lease_key: str) -> t.Optional[tuple[str, str, str, str]]:
    """Decompose a lease key, or ``None`` when it is malformed.

    Malformed keys are skipped rather than raising: one unparseable Redis entry
    (a different version's writer, a manual edit) must not abort a whole
    reconciliation pass over keys that are perfectly readable.
    """
    try:
        return split_lease_key(lease_key)
    except LeaseKeyError:
        logger.warning("lease_key_unparseable", lease_key=lease_key)
        return None


def _owner_of_worktree(worktree_path: str, members: t.Sequence[str]) -> str:
    """Member whose working copy this path is, for credential resolution."""
    from src.workspace.identity import workspace_base_path

    try:
        from src.workspace.identity import resolve_workspace_identity

        identity = resolve_workspace_identity(worktree_path)
        return identity.user_id
    except Exception:  # noqa: BLE001 - fall back to the first known member
        _ = workspace_base_path
        return members[0] if members else ""


_reconciler: t.Optional[EnvironmentReconciler] = None


def get_environment_reconciler() -> EnvironmentReconciler:
    """Get or create the global :class:`EnvironmentReconciler`."""
    global _reconciler
    if _reconciler is None:
        _reconciler = EnvironmentReconciler()
    return _reconciler
