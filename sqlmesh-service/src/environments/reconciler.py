"""Drift detection and repair across the registry, Git, SQLMesh and Dagster.

Why reconciliation is a first-class operation
--------------------------------------------
The registry is the source of truth, but the systems it drives live outside this
process: a Git remote, a warehouse, a Dagster service. Any of
them can be unavailable exactly when an orchestration needs it, and every write
path in this package is written to REPORT that instead of pretending it
succeeded — ``create`` marks an environment ``drifted`` when the virtual
environment could not be built, ``destroy`` reports the remote branch it could
not delete. Those honest partial outcomes are only useful if something later
finishes the job; this module is that something (design §11.1).

What it can and cannot decide
-----------------------------
Repairs are limited to actions that are unambiguous and cost no data:

* re-create a branch the registry owns, from the base it declares;
* re-create a virtual environment (a pointer switch, zero data);
* re-ensure a deployment the registry owns (a code location respawn — the
  Dagster-side registry is in-process state, so every dagster-service restart
  wipes it and this is how it comes back);
* release resources an ARCHIVED environment still holds (branches, views,
  deployments);
* move a ``drifted`` row back to ``active`` once nothing is wrong with it.

Everything that would compute data or adopt state nobody registered is REPORTED,
never performed: ``commit_drift`` needs an apply (the user chooses when to pay
for a backfill) and an ``orphan_branch`` needs a human to decide whether it
should become an environment at all (design §11.2).

Unreachable is not empty
------------------------
When the warehouse cannot be read, the SQLMesh side is reported as unknown and
every SQLMesh-related verdict is withheld; the same rule guards the Dagster
side when dagster-service cannot be read. Treating "cannot read" as "nothing
exists" would have the reconciler invalidate or rebuild every environment of
the workspace during a routine outage.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.environments import naming
from src.environments.deployment_binder import get_deployment_binder
from src.environments.git_binder import GitBinder
from src.environments.models import EnvironmentModel
from src.environments.repository import EnvironmentRepository
from src.environments.sqlmesh_binder import get_sqlmesh_binder
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

# Statuses whose environment is expected to be fully materialized. ``creating``
# and ``archiving`` are transitional: an orchestration is mid-flight and its
# resources are legitimately incomplete.
_LIVE_STATUSES = (
    naming.STATUS_ACTIVE,
    naming.STATUS_APPLYING,
    naming.STATUS_PROMOTING,
    naming.STATUS_DRIFTED,
)


@dataclass(frozen=True)
class Drift:
    """One divergence between the registry and a system it drives."""

    kind: str
    environment: str
    detail: str
    # Repairs are attempted only when the caller asked for them AND the drift is
    # one this module may act on; ``repaired`` is the honest outcome.
    repairable: bool = False
    repaired: bool = False
    repair_detail: str = ""

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "kind": self.kind,
            "environment": self.environment,
            "detail": self.detail,
            "repairable": self.repairable,
            "repaired": self.repaired,
            "repair_detail": self.repair_detail,
        }


@dataclass
class ReconcileReport:
    """Result of one reconciliation pass."""

    workspace_id: str
    scope: str
    checked_at: datetime
    # False when the SQLMesh state could not be read: every SQLMesh verdict is
    # withheld rather than guessed.
    sqlmesh_reachable: bool
    # Same rule for the orchestration side: False withholds BOTH deployment
    # verdicts instead of guessing what dagster-service serves.
    dagster_reachable: bool = True
    environments_checked: int = 0
    drifts: t.List[Drift] = field(default_factory=list)

    @property
    def repaired_count(self) -> int:
        return sum(1 for drift in self.drifts if drift.repaired)

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "workspace_id": self.workspace_id,
            "scope": self.scope,
            "checked_at": self.checked_at.isoformat(),
            "sqlmesh_reachable": self.sqlmesh_reachable,
            "dagster_reachable": self.dagster_reachable,
            "environments_checked": self.environments_checked,
            "drift_count": len(self.drifts),
            "repaired_count": self.repaired_count,
            "drifts": [drift.as_dict() for drift in self.drifts],
        }


class EnvironmentReconciler:
    """Compares the registry against Git and the SQLMesh state, and repairs."""

    def __init__(
        self,
        *,
        base_path: str,
        repository: t.Optional[EnvironmentRepository] = None,
        registry_factory: t.Optional[t.Callable[[], t.Any]] = None,
    ) -> None:
        self._git = GitBinder(base_path)
        self._sqlmesh = get_sqlmesh_binder()
        self._deployment = get_deployment_binder()
        self._repo = repository or EnvironmentRepository()
        self._registry_factory = registry_factory or _default_registry

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

        report = ReconcileReport(
            workspace_id=workspace.id,
            scope="workspace",
            checked_at=datetime.now(timezone.utc),
            sqlmesh_reachable=state is not None,
            dagster_reachable=served is not None,
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

        logger.info(
            "environment_reconcile_completed",
            workspace_id=workspace.id,
            scope="workspace",
            environments_checked=report.environments_checked,
            drift_count=len(report.drifts),
            repaired_count=report.repaired_count,
            sqlmesh_reachable=report.sqlmesh_reachable,
            dagster_reachable=report.dagster_reachable,
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
        """Reconcile one environment. Orphan detection is out of scope here."""
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
        repo = self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
        local, remote = await self._git.branch_presence(
            repo_path=repo, branch=model.branch
        )
        # No clone at all on this machine says nothing about the remote, so the
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

        # ``served is None`` means dagster-service could not be read: no
        # verdict follows, exactly like an unreachable warehouse above.
        if served is not None and _expected_deployment_name(model) not in served:
            report.drifts.append(
                await self._repair_missing_deployment(
                    model=model, workspace=workspace, members=members, repair=repair
                )
            )

        self._check_code_data_sync(model=model, members=members, report=report)

    def _check_code_data_sync(
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

        worktree = self._git.first_existing_worktree(
            tenant_id=model.tenant_id,
            workspace_id=model.workspace_id,
            environment=model.name,
            members=members,
        )
        if not worktree:
            return

        head = _head_of(worktree)
        if head and head != model.last_applied_commit:
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
        unreachable, when nobody held a working copy, or when dagster-service
        could not be reached. One ``release_archived_resources`` call finishes
        all of it; each drift kind then reports its own slice of the outcome.
        """
        repo = self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
        local, remote = await self._git.branch_presence(
            repo_path=repo, branch=model.branch
        )
        worktree = self._git.first_existing_worktree(
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
                    else "dagster-service refused the destroy"
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

        repo = self._git.first_existing_repo(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id,
            members=members,
        )
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

        worktree = self._git.first_existing_worktree(
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

        worktree = self._git.first_existing_worktree(
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
        cause is simply a dagster-service restart; the repair is exactly what
        the UI's lazy ensure does, only triggered by the platform instead of a
        page view, and it restores the row's POLICY (``env_type`` /
        ``schedules_enabled``) alongside the process.
        """
        name = _expected_deployment_name(model)
        detail = (
            f"environment is {model.status} but dagster-service serves no "
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
        worktree = self._git.first_existing_worktree(
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
                else "dagster-service refused the ensure"
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
            worktree = self._git.first_existing_worktree(
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
            worktree = self._git.first_existing_worktree(
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
        """Names of the deployments dagster-service serves FOR THIS WORKSPACE.

        One call per pass, shared by every environment check. The served list
        spans every workspace the service has loaded, so filtering by
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


def _head_of(worktree_path: str) -> str:
    """HEAD commit of a working copy, or ``""`` when it cannot be read."""
    from src.workspace.git_ops import GitWorktreeOperations

    return GitWorktreeOperations.head_commit(worktree_path)


def _owner_of_worktree(worktree_path: str, members: t.Sequence[str]) -> str:
    """Member whose working copy this path is, for credential resolution."""
    from src.workspace import paths as ws_paths

    try:
        _, _, user_id, _ = ws_paths.parse_workspace_path(
            _base_of(worktree_path), worktree_path
        )
        return user_id
    except Exception:  # noqa: BLE001 - fall back to the first known member
        return members[0] if members else ""


def _base_of(worktree_path: str) -> str:
    """Workspace base path, as configured for this process."""
    from src.workspace.identity import workspace_base_path

    return workspace_base_path()


_reconciler: t.Optional[EnvironmentReconciler] = None


def get_environment_reconciler() -> EnvironmentReconciler:
    """Get or create the global :class:`EnvironmentReconciler`."""
    global _reconciler
    if _reconciler is None:
        import os

        from src.config import settings

        _reconciler = EnvironmentReconciler(
            base_path=os.path.expanduser(settings.workspace_base_path)
        )
    return _reconciler
