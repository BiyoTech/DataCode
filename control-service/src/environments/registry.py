"""Environment registry — orchestration and lifecycle state machine.

The registry is the platform's source of truth for environments. Every
operation writes the registry FIRST and then drives the downstream systems, so
an interrupted operation is always replayable and never leaves an
unaccounted-for resource.

Downstream binders (P5: all dispatched to workspace-runtime over HTTP):

* Git (branch + worktree)  — :mod:`src.environments.git_binder`
* SQLMesh virtual env      — :mod:`src.environments.sqlmesh_binder`
* Dagster deployment       — :mod:`src.environments.deployment_binder`

Authorization is not performed here: the gateway is the single authorization
center. This module enforces tenant scoping and the domain invariants that do
not depend on roles.
"""

from __future__ import annotations

import time
import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from datacode_common import naming

from src.config import settings
from src.core.env_locks import environment_lock_by_key
from src.environments.deployment_binder import get_deployment_binder
from src.environments.git_binder import (
    BRANCH_SOURCE_ADOPTED_LOCAL,
    BRANCH_SOURCE_REUSED_REMOTE,
    GitBinder,
    PendingWork,
)
from src.environments.models import EnvironmentModel
from src.environments.repository import EnvironmentRepository
from src.environments.sqlmesh_binder import get_sqlmesh_binder
from src.utils.errors import (
    ConflictError,
    EnvironmentNotFoundError,
    EnvironmentStateError,
    InvalidRequestError,
    WorkspaceNotFoundError,
)
from src.utils.logging import get_logger
from src.workspace.models import WorkspaceModel
from src.workspace.repository import WorkspaceRepository

logger = get_logger(__name__)

# Environment types a user may create. ``production`` is never created by hand:
# it is the workspace's own environment, created together with the workspace.
CREATABLE_ENV_TYPES = (naming.ENV_TYPE_SHARED, naming.ENV_TYPE_SANDBOX)

# TTL bounds for sandbox environments (hours). A sandbox always expires so the
# platform can reclaim state, schemas and deployments.
MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 720  # 30 days

# Statuses from which a working copy may be materialized.
OPENABLE_STATUSES = (
    naming.STATUS_ACTIVE,
    naming.STATUS_APPLYING,
    naming.STATUS_PROMOTING,
    naming.STATUS_DRIFTED,
)

# The only workspace status that may gain new working copies. Archived and
# deleted workspaces keep their rows for audit but are not operable.
WORKSPACE_STATUS_ACTIVE = "active"

# Statuses whose row holds no live resource, so its name may be claimed again by
# a new create. Recycling the row (instead of rejecting the name) is what makes
# a failed creation retryable and a destroyed sandbox recreatable under the same
# name, which the naming convention actively encourages.
RECREATABLE_STATUSES = (naming.STATUS_ARCHIVED, naming.STATUS_FAILED)


@dataclass(frozen=True)
class EnvironmentView:
    """Read model handed to the API layer. All timestamps are UTC."""

    environment_id: str
    workspace_id: str
    tenant_id: str
    name: str
    branch: str
    env_type: str
    base_env: str
    owner_user_id: str
    status: str
    deployment_name: str
    schedules_enabled: bool
    is_production: bool
    last_applied_commit: str
    last_applied_at: t.Optional[datetime]
    # True when the last apply ran from a working copy with uncommitted
    # changes: the data is not reproducible from ``last_applied_commit``.
    last_applied_dirty: bool
    last_promoted_to: str
    ttl_hours: int
    expires_at: t.Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CreatedEnvironment:
    """An environment plus the one fact that is true only of its creation.

    ``branch_source`` describes what THIS call did, not what the row is, so it
    is deliberately kept out of ``EnvironmentView``: a list endpoint replaying
    it later would be reporting a stale decision as current state.
    """

    environment: EnvironmentView
    branch_source: str


@dataclass(frozen=True)
class OpenedEnvironment:
    """Result of materializing one environment working copy for one user."""

    environment_id: str
    environment: str
    branch: str
    workspace_path: str
    head_commit: str
    materialized: bool


@dataclass(frozen=True)
class MemberPendingWork:
    """One member's share of the work a destroy would lose."""

    user_id: str
    uncommitted: int
    unpushed: int


@dataclass(frozen=True)
class PendingWorkReport:
    """Pre-flight answer for the delete dialog.

    Read-only and lock-free by design: it is consulted BEFORE the user
    confirms, so it must never queue behind an apply, and it is never a
    precondition of ``destroy`` — a failed probe degrades the dialog's warning,
    it does not block the deletion.
    """

    environment_id: str
    branch: str
    uncommitted: int
    unpushed: int
    members: t.Tuple[MemberPendingWork, ...]


@dataclass(frozen=True)
class ReleasedResources:
    """What a release actually managed to free.

    A skipped release (no local copy anywhere, a missing credential) must not
    masquerade as a full cleanup, so every resource reports its own outcome and
    the caller learns exactly what the reconciler still has to finish.

    The remote branch is deliberately absent: the platform never deletes it, so
    reporting an outcome for it would describe an operation that no longer
    exists.
    """

    virtual_environment_released: bool
    # Number of member working copies released. An environment is held by one
    # worktree PER MEMBER, so this is the honest measure of the local cleanup.
    worktrees_removed: int
    # False when the runtime was unreachable or refused: the deployment
    # subprocess keeps running until the reconciler finishes the release.
    deployment_destroyed: bool = False


@dataclass(frozen=True)
class DestroyResult(ReleasedResources):
    """Outcome of a destroy: the resources freed plus the lifecycle transition."""

    archived: bool = False
    # The branch that was KEPT. Named so a client can tell the user what
    # survived (and what to reuse) without having to know the platform policy.
    preserved_branch: str = ""
    # Commits that existed only in a working copy and are now gone. Reported,
    # never a reason to refuse — the user was warned before confirming.
    unpushed_commits_discarded: int = 0


class EnvironmentRegistry:
    """CRUD + lifecycle orchestration for platform environments."""

    def __init__(self, base_path: str = "") -> None:
        # ``base_path`` retained for call-site compatibility; the HTTP binder
        # derives paths from settings itself.
        self._git = GitBinder(base_path)
        self._sqlmesh = get_sqlmesh_binder()
        self._deployment = get_deployment_binder()
        self._repo = EnvironmentRepository()
        self._workspaces = WorkspaceRepository()

    # ---- Queries ----

    async def list(
        self, workspace_id: str, tenant_id: str, include_archived: bool = False
    ) -> t.List[EnvironmentView]:
        """List a workspace's environments (shared across all its members)."""
        await self._require_workspace(workspace_id, tenant_id)
        models = await self._repo.list_by_workspace(workspace_id, include_archived)
        logger.info(
            "environments_listed",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            count=len(models),
        )
        return [_to_view(model) for model in models]

    async def get(self, environment_id: str, tenant_id: str) -> EnvironmentView:
        """Get one environment inside a tenant, or raise 404."""
        return _to_view(await self._require_environment(environment_id, tenant_id))

    async def events(
        self, environment_id: str, tenant_id: str, limit: int = 50
    ) -> t.List[t.Dict[str, t.Any]]:
        """Lifecycle timeline of an environment (newest first)."""
        await self._require_environment(environment_id, tenant_id)
        events = await self._repo.list_events(environment_id, limit)
        return [
            {
                "id": event.id,
                "action": event.action,
                "actor_user_id": event.actor_user_id,
                "trigger_type": event.trigger_type,
                "commit_hash": event.commit_hash or "",
                "succeeded": bool(event.succeeded),
                "detail_json": event.detail_json or "",
                "duration_ms": event.duration_ms,
                "created_at": event.created_at,
            }
            for event in events
        ]

    # ---- Creation ----

    async def ensure_production(
        self, workspace: WorkspaceModel, owner_user_id: str = ""
    ) -> EnvironmentView:
        """Register the workspace's production environment if it is missing.

        Called when a workspace is created: production is not a user-created
        environment, it is the workspace's own ``main`` branch. Idempotent so a
        retried workspace creation converges.

        There is no "reclaim a previous workspace's production row" case anymore:
        deleting a workspace physically removes its environment rows, so a
        workspace recreated under the same name starts from a genuinely empty
        registry. That used to be a special path, and it was the source of the
        "Environment prod cannot be opened while it is archived" failure — a row
        that outlived its workspace and then blocked the name.

        A row in any non-openable state therefore means a CONCURRENT create is
        mid-flight (or one died leaving ``creating`` behind), which is a conflict
        to report rather than something to overwrite.
        """
        name = workspace.production_env or naming.PRODUCTION_ENV
        new_owner = owner_user_id or workspace.owner_user_id
        existing = await self._repo.get_by_name(workspace.id, name)

        if existing is None:
            model = EnvironmentModel(
                id=naming.environment_id(workspace.id, name),
                workspace_id=workspace.id,
                tenant_id=workspace.tenant_id,
                name=name,
                branch=naming.branch_of(name),
                env_type=naming.ENV_TYPE_PRODUCTION,
                base_env=name,
                owner_user_id=new_owner,
                status=naming.STATUS_ACTIVE,
                deployment_name=naming.deployment_name(workspace.id, name),
                # Production is the only environment allowed to run schedules.
                schedules_enabled=1,
                ttl_hours=0,
                expires_at=None,
            )
            await self._repo.create(model)
            await self._record_event(
                model.id,
                naming.ACTION_CREATE,
                actor_user_id=new_owner,
                detail={"env_type": naming.ENV_TYPE_PRODUCTION, "bootstrap": True},
            )
            logger.info(
                "production_environment_registered",
                workspace_id=workspace.id,
                environment=name,
            )
            return _to_view(model)

        # The row exists. If it is already operable, no work is needed.
        if existing.status in OPENABLE_STATUSES:
            return _to_view(existing)

        raise ConflictError(
            f"Production environment {existing.id} is {existing.status}; "
            "wait for the current operation to settle before recreating "
            "the workspace"
        )

    async def create(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        actor_user_id: str,
        name: str,
        env_type: str = naming.ENV_TYPE_SANDBOX,
        base_env: str = "",
        ttl_hours: int = 0,
        trigger_type: str = "human",
    ) -> CreatedEnvironment:
        """Create an environment: registry row, Git branch and worktree.

        Order matters: the row is inserted as ``creating`` before any Git write,
        so a crash in the middle is visible and retryable instead of leaving an
        orphan branch nobody accounts for.

        An environment whose branch still exists on the remote — because a
        previous environment of this name was destroyed, or a teammate pushed
        it — RESUMES that branch instead of re-cutting one from the base. The
        outcome is reported through ``branch_source`` so the caller can tell
        the user which of the two happened.

        Serialized by the environment lock, like apply and promote (design
        §6.3): without it a concurrent ``destroy`` could release the branch and
        the worktree while this orchestration is still building them, and the
        final status write would then resurrect an archived row whose resources
        no longer exist.
        """
        workspace = await self._require_workspace(workspace_id, tenant_id)

        name = (name or "").strip().lower()
        if not naming.is_valid_env_name(name):
            raise InvalidRequestError(
                "Environment name must be lowercase snake_case, 2-40 characters, "
                "starting with a letter"
            )
        production_name = workspace.production_env or naming.PRODUCTION_ENV
        if name == production_name or name == naming.PRODUCTION_ENV:
            raise ConflictError(
                "The production environment is created together with the workspace"
            )
        if env_type not in CREATABLE_ENV_TYPES:
            raise InvalidRequestError(
                f"env_type must be one of {list(CREATABLE_ENV_TYPES)}"
            )

        async with environment_lock_by_key(
            workspace_id, name, operation="create"
        ):
            return await self._create_locked(
                workspace=workspace,
                actor_user_id=actor_user_id,
                name=name,
                env_type=env_type,
                base_env=base_env or production_name,
                production_name=production_name,
                ttl_hours=ttl_hours,
                trigger_type=trigger_type,
            )

    async def _create_locked(
        self,
        *,
        workspace: WorkspaceModel,
        actor_user_id: str,
        name: str,
        env_type: str,
        base_env: str,
        production_name: str,
        ttl_hours: int,
        trigger_type: str,
    ) -> CreatedEnvironment:
        """Run the creation orchestration; the caller holds the environment lock."""
        started = time.monotonic()
        workspace_id = workspace.id
        tenant_id = workspace.tenant_id

        base = await self._resolve_base(workspace, base_env)
        await self._assert_quota(workspace_id)
        # Tenant-level cap, complementing the per-workspace one above: a tenant
        # spreading one sandbox over each workspace would pass every local
        # limit while exhausting the platform's branch/deployment capacity.
        from src.quota import get_quota_service

        await get_quota_service().check_environment_creation(tenant_id)
        existing = await self._repo.get_by_name(workspace_id, name)
        if existing is not None and existing.status not in RECREATABLE_STATUSES:
            raise ConflictError(f"Environment already exists: {name}")

        environment_id = naming.environment_id(workspace_id, name)
        expires_at, ttl = _expiry_for(env_type, ttl_hours)
        model = EnvironmentModel(
            id=environment_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            name=name,
            branch=naming.branch_of(name),
            env_type=env_type,
            base_env=base.name,
            owner_user_id=actor_user_id,
            status=naming.STATUS_CREATING,
            deployment_name=naming.deployment_name(workspace_id, name),
            # Branch deployments never schedule by default: the same code would
            # otherwise drive production data on every tick.
            schedules_enabled=0,
            ttl_hours=ttl,
            expires_at=expires_at,
        )
        if existing is not None:
            # Recycle the dead row instead of rejecting the name. Two cases
            # reach here and both must be recoverable through a plain retry
            # (design §6.2 requires create to be idempotent):
            #   archived - the environment was destroyed, and the sandbox naming
            #              convention makes users recreate the very same name;
            #   failed   - a previous attempt died halfway, leaving nothing but
            #              this row; refusing it would strand the name forever.
            # Identity columns stay untouched, so the previous life's audit
            # trail remains attached to the row.
            await self._repo.reactivate(
                environment_id,
                expected_statuses=RECREATABLE_STATUSES,
                env_type=env_type,
                base_env=base.name,
                owner_user_id=actor_user_id,
                deployment_name=naming.deployment_name(workspace_id, name),
                schedules_enabled=0,
                ttl_hours=ttl,
                expires_at=expires_at,
            )
        else:
            await self._repo.create(model)

        try:
            await self._git.ensure_repository(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=actor_user_id,
                git_repo_url=workspace.git_repo_url or "",
                bootstrap_branch=naming.branch_of(production_name),
            )
            materialization = await self._git.ensure_worktree(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=actor_user_id,
                environment=name,
                branch=model.branch,
                start_branch=base.branch,
            )
            if materialization.branch_source == BRANCH_SOURCE_REUSED_REMOTE:
                # Already on the remote — that is where this worktree came
                # from. Pushing would be a round trip to tell the remote what
                # it just told us.
                published = True
            else:
                published = await self._publish_branch(
                    workspace=workspace,
                    worktree_path=materialization.workspace_path,
                    branch=model.branch,
                    user_id=actor_user_id,
                )
        except Exception as exc:
            await self._repo.update_status(
                environment_id,
                naming.STATUS_FAILED,
                expected=naming.STATUS_CREATING,
            )
            await self._record_event(
                environment_id,
                naming.ACTION_CREATE,
                actor_user_id=actor_user_id,
                trigger_type=trigger_type,
                succeeded=False,
                detail={"error": str(exc)},
                duration_ms=_elapsed_ms(started),
            )
            logger.error(
                "environment_create_failed",
                workspace_id=workspace_id,
                environment=name,
                error=str(exc),
            )
            raise

        # A branch that failed to publish exists only in the creator's clone:
        # another member's ``open`` would fail with BRANCH_NOT_FOUND, so the
        # drift is reported through the status instead of leaving a silently
        # broken environment behind.
        status = naming.STATUS_ACTIVE
        if not published and workspace.git_repo_url:
            status = naming.STATUS_DRIFTED
            await self._record_event(
                environment_id,
                naming.ACTION_CREATE,
                actor_user_id=actor_user_id,
                trigger_type=trigger_type,
                succeeded=False,
                detail={"stage": "branch_publish", "branch": model.branch},
                duration_ms=_elapsed_ms(started),
            )
            logger.warning(
                "environment_branch_publish_drifted",
                workspace_id=workspace_id,
                environment=name,
                branch=model.branch,
            )

        # Compare-and-set: only a row still in ``creating`` may be published as
        # active. Anything else means another operation took over (a destroy
        # that ran while the branch was being built), and overwriting it would
        # resurrect an environment whose resources are already released.
        if not await self._repo.update_status(
            environment_id, status, expected=naming.STATUS_CREATING
        ):
            current = await self._repo.get(environment_id)
            logger.warning(
                "environment_create_superseded",
                workspace_id=workspace_id,
                environment=name,
                observed_status=current.status if current else "missing",
            )
            raise ConflictError(
                f"Environment {name} was modified while it was being created; "
                "inspect its state and retry"
            )
        model.status = status
        await self._record_event(
            environment_id,
            naming.ACTION_CREATE,
            actor_user_id=actor_user_id,
            trigger_type=trigger_type,
            commit_hash=materialization.commit,
            detail={
                "env_type": env_type,
                "base_env": base.name,
                "branch": model.branch,
                "branch_published": published,
                "branch_source": materialization.branch_source,
                "initial_status": status,
            },
            duration_ms=_elapsed_ms(started),
        )
        logger.info(
            "environment_created",
            workspace_id=workspace_id,
            environment=name,
            env_type=env_type,
            base_env=base.name,
            branch=model.branch,
            branch_published=published,
            branch_source=materialization.branch_source,
            initial_status=status,
        )
        if materialization.branch_source == BRANCH_SOURCE_REUSED_REMOTE:
            # The user gets their previous code back rather than a fresh copy
            # of the base. Worth its own line: "created" alone would hide the
            # single most surprising thing about this environment.
            logger.info(
                "environment_branch_reused",
                workspace_id=workspace_id,
                environment=name,
                branch=model.branch,
                commit=materialization.commit[:8],
            )
        elif materialization.branch_source == BRANCH_SOURCE_ADOPTED_LOCAL:
            logger.warning(
                "environment_branch_adopted_local",
                workspace_id=workspace_id,
                environment=name,
                branch=model.branch,
                commit=materialization.commit[:8],
            )

        # The virtual environment is a zero-cost pointer switch, but it needs the
        # warehouse to be reachable. A failure here is drift, not a lost
        # environment: the branch and worktree exist, so the environment stays
        # usable for code work and the reconciler repairs the SQLMesh side.
        virtual_ready = await self._sqlmesh.ensure_virtual_environment(
            workspace_path=materialization.workspace_path,
            environment=name,
            base_env=base.name,
        )
        if not virtual_ready:
            await self._repo.update_status(
                environment_id, naming.STATUS_DRIFTED, expected=status
            )
            model.status = naming.STATUS_DRIFTED
            await self._record_event(
                environment_id,
                naming.ACTION_CREATE,
                actor_user_id=actor_user_id,
                trigger_type=trigger_type,
                succeeded=False,
                detail={
                    "stage": "sqlmesh_virtual_environment",
                    "base_env": base.name,
                },
                duration_ms=_elapsed_ms(started),
            )
            logger.warning(
                "environment_virtual_env_missing",
                workspace_id=workspace_id,
                environment=name,
                base_env=base.name,
            )

        # Bring the deployment up from the creator's freshly materialized
        # working copy, carrying the row's frozen policy. Best-effort BY DESIGN:
        # a deployment is zero-data derived state, so an unreachable
        # runtime does NOT mark the environment drifted (unlike the
        # virtual environment above, whose absence is data-plane drift) — the
        # reconciler's ``missing_deployment`` repair rebuilds it later.
        deployment_ready = await self._deployment.ensure_deployment(
            workspace_path=materialization.workspace_path,
            environment=name,
            env_type=env_type,
            # The row was just frozen with schedules off: branch deployments
            # never schedule by default (see the model construction above).
            schedules_enabled=False,
        )
        if not deployment_ready:
            await self._record_event(
                environment_id,
                naming.ACTION_CREATE,
                actor_user_id=actor_user_id,
                trigger_type=trigger_type,
                succeeded=False,
                detail={"stage": "dagster_deployment"},
                duration_ms=_elapsed_ms(started),
            )
            logger.warning(
                "environment_deployment_ensure_failed",
                workspace_id=workspace_id,
                environment=name,
            )

        # Read the row back instead of serializing the in-memory model: on the
        # recycled-row path the model was never inserted, so its server-side
        # defaults (created_at / updated_at) would still be unset.
        persisted = await self._repo.get(environment_id, tenant_id)
        return CreatedEnvironment(
            environment=_to_view(persisted if persisted is not None else model),
            branch_source=materialization.branch_source,
        )

    # ---- Working copies ----

    async def open(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        user_id: str,
        allow_bootstrap_commit: bool = False,
    ) -> OpenedEnvironment:
        """Materialize ``user_id``'s worktree of an environment.

        Every member holds their own worktree of the same branch; conflicts
        between members are ordinary Git conflicts, resolved with ordinary Git.

        ``allow_bootstrap_commit`` is only ever set by workspace creation, which
        is the one caller that KNOWS the bound remote carries no branch (it just
        created it, or probed it as empty). Opening an existing workspace must
        never set it: an empty root commit seeded because refs merely failed to
        fetch would fork the repository's history.
        """
        model = await self._require_environment(environment_id, tenant_id)
        if model.status not in OPENABLE_STATUSES:
            raise EnvironmentStateError(
                f"Environment {model.name} cannot be opened while it is {model.status}"
            )

        workspace = await self._require_workspace(model.workspace_id, tenant_id)
        if workspace.status != WORKSPACE_STATUS_ACTIVE:
            # An archived workspace keeps its rows for audit but must not gain
            # new working copies: materializing one would resurrect activity on
            # a workspace the tenant has retired.
            raise EnvironmentStateError(
                f"Workspace {workspace.id} is {workspace.status}; "
                "its environments cannot be opened"
            )
        await self._git.ensure_repository(
            tenant_id=tenant_id,
            workspace_id=model.workspace_id,
            user_id=user_id,
            git_repo_url=workspace.git_repo_url or "",
            bootstrap_branch=naming.branch_of(
                workspace.production_env or naming.PRODUCTION_ENV
            ),
            allow_bootstrap_commit=allow_bootstrap_commit,
        )
        materialization = await self._git.ensure_worktree(
            tenant_id=tenant_id,
            workspace_id=model.workspace_id,
            user_id=user_id,
            environment=model.name,
            branch=model.branch,
        )

        await self._touch_workspace(model.workspace_id)
        if materialization.created:
            # Only the first materialization is audited: re-opening is a read.
            await self._record_event(
                environment_id,
                naming.ACTION_OPEN,
                actor_user_id=user_id,
                commit_hash=materialization.commit,
                detail={"workspace_path": materialization.workspace_path},
            )

        logger.info(
            "environment_opened",
            environment_id=environment_id,
            user_id=user_id,
            branch=model.branch,
            materialized=materialization.created,
        )
        return OpenedEnvironment(
            environment_id=environment_id,
            environment=model.name,
            branch=model.branch,
            workspace_path=materialization.workspace_path,
            head_commit=materialization.commit,
            materialized=materialization.created,
        )

    # ---- Reconciliation ----

    async def reconcile(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        actor_user_id: str,
        repair: bool = True,
    ) -> t.Dict[str, t.Any]:
        """Reconcile one environment against Git and the SQLMesh state.

        Serialized with the other state-mutating operations when it may repair:
        recreating a branch or a virtual environment while an apply is running
        would race on exactly the resources that apply is using. A read-only
        pass (``repair=False``) needs no lock.
        """
        from src.environments.reconciler import get_environment_reconciler

        model = await self._require_environment(environment_id, tenant_id)
        workspace = await self._require_workspace(model.workspace_id, tenant_id)
        reconciler = get_environment_reconciler()

        if not repair:
            report = await reconciler.reconcile_environment(
                model=model,
                workspace=workspace,
                actor_user_id=actor_user_id,
                repair=False,
            )
            return report.as_dict()

        # The outer lock serializes the LIVE repairs — recreating a branch, a
        # virtual environment or a deployment takes no lock of its own. A
        # non-live row is EXCLUDED on purpose: its repair goes through
        # ``release_archived_resources``, which acquires this very lock
        # internally, and the lock is not reentrant — holding it here would
        # make every archived repair fail with ENVIRONMENT_BUSY.
        if model.status in OPENABLE_STATUSES:
            async with environment_lock_by_key(
                model.workspace_id, model.name, operation="reconcile"
            ):
                report = await reconciler.reconcile_environment(
                    model=model,
                    workspace=workspace,
                    actor_user_id=actor_user_id,
                    repair=True,
                )
        else:
            report = await reconciler.reconcile_environment(
                model=model,
                workspace=workspace,
                actor_user_id=actor_user_id,
                repair=True,
            )
        return report.as_dict()

    # ---- Destruction ----

    async def pending_work(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        actor_user_id: str,
    ) -> PendingWorkReport:
        """What destroying this environment would lose, without destroying it.

        Exists so the warning reaches the user while they can still act on it:
        answering "you have N unpushed commits" inside a destroy's error
        response would mean the request had already been refused, which is the
        shape of a block, not of a hint.

        Takes NO environment lock. It reads working copies that a concurrent
        apply may be writing, so its counts are a snapshot, not a guarantee —
        and that is the right trade: a dialog must never wait behind a
        backfill, and ``destroy`` re-probes under the lock anyway.
        """
        model = await self._require_environment(environment_id, tenant_id)
        members = await self._members_for_release(
            tenant_id=tenant_id,
            workspace_id=model.workspace_id,
            actor_user_id=actor_user_id,
        )
        pending = await self._collect_pending_work(
            tenant_id=tenant_id,
            workspace_id=model.workspace_id,
            environment=model.name,
            branch=model.branch,
            members=members,
        )
        return PendingWorkReport(
            environment_id=environment_id,
            branch=model.branch,
            uncommitted=sum(work.uncommitted for work in pending.values()),
            unpushed=sum(work.unpushed for work in pending.values()),
            members=tuple(
                MemberPendingWork(
                    user_id=member,
                    uncommitted=work.uncommitted,
                    unpushed=work.unpushed,
                )
                for member, work in sorted(pending.items())
            ),
        )

    async def destroy(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        actor_user_id: str,
        force: bool = False,
        trigger_type: str = "human",
    ) -> DestroyResult:
        """Archive an environment and release the state the PLATFORM owns.

        Serialized by the environment lock exactly like apply and promote
        (design §6.3): destroying mid-apply would rip the worktree out from
        under a running backfill and orphan the virtual environment. A busy
        environment answers 409 instead.

        Releases EVERY member's copy, not just the caller's: an environment is
        materialized once per member, so a partial cleanup would leave others
        holding a worktree of an environment that no longer exists.

        What is NOT released: the remote branch. The remote repository belongs
        to the user — it may predate the workspace, be shared with tooling
        outside DataCode, and hold the only durable copy of the code. It is
        also what lets a same-named environment resume this line of
        development later (``_add_worktree`` tracks it).

        Uncommitted changes in ANY member's copy are preserved by an explicit
        confirmation unless ``force`` is set, because worktree removal discards
        them (design §13).

        The registry row and its events are KEPT (audit); only the resources
        are released, in dependency order: the SQLMesh virtual environment
        first (it needs a working copy to reach the engine), then Git, and the
        Dagster deployment last (it is the most-derived resource — rebuilt
        FROM a working copy, so nothing downstream depends on releasing it
        first, and an unreachable runtime leaves it to the reconciler).
        """
        started = time.monotonic()
        model = await self._require_environment(environment_id, tenant_id)
        if model.env_type == naming.ENV_TYPE_PRODUCTION:
            raise EnvironmentStateError(
                "The production environment cannot be destroyed"
            )
        if model.status == naming.STATUS_ARCHIVED:
            return DestroyResult(
                archived=False,
                virtual_environment_released=False,
                worktrees_removed=0,
                deployment_destroyed=False,
                preserved_branch=model.branch,
            )

        # No workspace lookup: the environment row is already tenant-scoped by
        # ``_require_environment``, and nothing released below is addressed by
        # the remote URL anymore.
        async with environment_lock_by_key(
            model.workspace_id, model.name, operation="destroy"
        ):
            # The caller's copy comes first: it is the one whose engine
            # connection and credentials are known to be the caller's own.
            members = await self._members_for_release(
                tenant_id=tenant_id,
                workspace_id=model.workspace_id,
                actor_user_id=actor_user_id,
            )

            # Probed even under ``force``: the counts are what makes the audit
            # trail able to answer "how much did that reclaim actually throw
            # away", and force only removes the refusal, not the record.
            pending = await self._collect_pending_work(
                tenant_id=tenant_id,
                workspace_id=model.workspace_id,
                environment=model.name,
                branch=model.branch,
                members=members,
            )
            if not force:
                self._assert_no_uncommitted_work(
                    pending, model.name, model.workspace_id
                )
            unpushed_discarded = sum(work.unpushed for work in pending.values())

            await self._repo.update_status(environment_id, naming.STATUS_ARCHIVING)

            released = await self._release_all_resources(
                model=model,
                members=members,
            )
            virtual_env_released = released.virtual_environment_released
            worktrees_removed = released.worktrees_removed
            deployment_destroyed = released.deployment_destroyed

            await self._repo.update_status(environment_id, naming.STATUS_ARCHIVED)
            await self._record_event(
                environment_id,
                naming.ACTION_DESTROY,
                actor_user_id=actor_user_id,
                trigger_type=trigger_type,
                detail={
                    "remote_branch_preserved": model.branch,
                    "virtual_environment_released": virtual_env_released,
                    "worktrees_removed": worktrees_removed,
                    "deployment_destroyed": deployment_destroyed,
                    "members_scanned": len(members),
                    "unpushed_commits_discarded": unpushed_discarded,
                    "forced": force,
                },
                duration_ms=_elapsed_ms(started),
            )
            logger.info(
                "environment_destroyed",
                environment_id=environment_id,
                actor_user_id=actor_user_id,
                virtual_environment_released=virtual_env_released,
                worktrees_removed=worktrees_removed,
                deployment_destroyed=deployment_destroyed,
                members_scanned=len(members),
            )
            # Stated explicitly rather than left to be inferred from the
            # absence of a deletion: operators reading the log must be able to
            # see that the branch survives this destroy on purpose.
            logger.info(
                "environment_remote_branch_preserved",
                environment_id=environment_id,
                branch=model.branch,
            )
            if unpushed_discarded:
                # The one piece of code this destroy really did lose. Warned
                # rather than refused (the user was told before confirming),
                # but never left unrecorded.
                logger.warning(
                    "environment_unpushed_commits_discarded",
                    environment_id=environment_id,
                    branch=model.branch,
                    commits=unpushed_discarded,
                    members=sorted(pending),
                )
            return DestroyResult(
                archived=True,
                virtual_environment_released=virtual_env_released,
                worktrees_removed=worktrees_removed,
                deployment_destroyed=deployment_destroyed,
                preserved_branch=model.branch,
                unpushed_commits_discarded=unpushed_discarded,
            )

    async def release_all_for_workspace(
        self, *, workspace_id: str, tenant_id: str, actor_user_id: str
    ) -> t.Dict[str, t.Any]:
        """Release every resource of every environment of a workspace being deleted.

        Not ``destroy`` per environment, for two reasons that both matter here:

        * ``destroy`` REFUSES production, because destroying it while the
          workspace lives would leave a workspace with no production data view.
          When the workspace itself is going away that protection is exactly
          backwards — production is the one environment guaranteed to exist, so
          skipping it would strand its deployment and views;
        * ``destroy`` protects uncommitted work with a confirmation. The user has
          already confirmed the deletion of the whole workspace; asking again per
          environment would be a second dialog for the same decision.

        The remote repository is untouched, as it is by every other release
        path: its branches belong to the user, not to the workspace row. The
        local rows are removed by the caller afterwards.

        Every environment is released independently: one unreachable resource
        must not strand the rest, and what fails here is picked up by the
        reconciler (or is simply gone with the workspace).
        """
        released_environments = 0
        failures = 0
        for model in await self._repo.list_by_workspace(
            workspace_id, include_archived=True
        ):
            try:
                members = await self._members_for_release(
                    tenant_id=tenant_id,
                    workspace_id=model.workspace_id,
                    actor_user_id=actor_user_id,
                )
                await self._release_all_resources(
                    model=model,
                    members=members,
                )
                released_environments += 1
            except Exception as exc:  # noqa: BLE001 - never block the deletion
                failures += 1
                logger.warning(
                    "environment_release_on_workspace_delete_failed",
                    environment_id=model.id,
                    error=str(exc),
                )

        logger.info(
            "environments_released_for_workspace",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            released=released_environments,
            failures=failures,
        )
        return {"released": released_environments, "failures": failures}

    async def purge_all_for_workspace(self, *, workspace_id: str) -> int:
        """Physically delete the workspace's environment rows.

        Called after the resources are released. Rows rather than statuses,
        because an archived row that outlives its workspace is what made
        recreating the same workspace name fail with "Environment prod cannot be
        opened while it is archived". ``environment_events`` are kept: an audit
        trail that disappears with its subject records nothing.
        """
        return await self._repo.delete_by_workspace(workspace_id)

    # ---- Internals ----

    async def _require_workspace(
        self, workspace_id: str, tenant_id: str
    ) -> WorkspaceModel:
        """Resolve a workspace inside a tenant, or raise 404.

        Cross-tenant identifiers answer 404 exactly like unknown ones, so they
        cannot be enumerated.
        """
        workspace = await self._workspaces.get_in_tenant(workspace_id, tenant_id)
        if workspace is None:
            logger.warning(
                "environment_workspace_not_found",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
            )
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def _require_environment(
        self, environment_id: str, tenant_id: str
    ) -> EnvironmentModel:
        """Resolve an environment inside a tenant, or raise 404."""
        workspace_id, name = naming.split_environment_id(environment_id)
        if not workspace_id or not name:
            raise EnvironmentNotFoundError(environment_id)

        model = await self._repo.get(environment_id, tenant_id)
        if model is None:
            logger.warning(
                "environment_not_found",
                environment_id=environment_id,
                tenant_id=tenant_id,
            )
            raise EnvironmentNotFoundError(environment_id)
        return model

    async def _resolve_base(
        self, workspace: WorkspaceModel, base_env: str
    ) -> EnvironmentModel:
        """Resolve the base environment a new environment branches from."""
        base = await self._repo.get_by_name(workspace.id, base_env)
        if base is None:
            raise InvalidRequestError(f"Base environment does not exist: {base_env}")
        if base.status not in OPENABLE_STATUSES:
            raise EnvironmentStateError(
                f"Base environment {base_env} is {base.status} and cannot be branched"
            )
        return base

    async def _assert_quota(self, workspace_id: str) -> None:
        """Cap the number of live environments per workspace.

        Each environment costs a branch, a virtual environment, a set of schemas
        and a Dagster deployment, so the count must stay bounded.
        """
        live = await self._repo.list_by_workspace(workspace_id)
        limit = settings.max_environments_per_workspace
        if len(live) >= limit:
            raise ConflictError(
                f"Workspace reached its environment limit ({limit}); "
                "destroy an unused environment first"
            )

    async def release_archived_resources(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        actor_user_id: str,
    ) -> ReleasedResources:
        """Release resources still held by an ARCHIVED environment.

        ``destroy`` reports honestly what it could not finish — a virtual
        environment nobody had a working copy for, a member copy that failed to
        unmount, a deployment the runtime refused to stop. This is the
        operation that finishes the job later, and it is what the reconciler
        calls when it finds an archived row still backed by live resources.

        The remote branch is not part of that job: it is preserved by design,
        so an archived row whose branch still exists is in its EXPECTED state,
        not an unfinished cleanup.

        Refuses anything that is NOT archived: releasing the resources of a live
        environment is ``destroy``, which additionally protects uncommitted work
        and settles the lifecycle state.
        """
        model = await self._require_environment(environment_id, tenant_id)
        if model.status != naming.STATUS_ARCHIVED:
            raise EnvironmentStateError(
                f"Environment {model.name} is {model.status}, not archived; "
                "use destroy to release a live environment"
            )

        async with environment_lock_by_key(
            model.workspace_id, model.name, operation="release"
        ):
            members = await self._members_for_release(
                tenant_id=tenant_id,
                workspace_id=model.workspace_id,
                actor_user_id=actor_user_id,
            )
            released = await self._release_all_resources(
                model=model,
                members=members,
            )

        logger.info(
            "environment_archived_resources_released",
            environment_id=environment_id,
            worktrees_removed=released.worktrees_removed,
            virtual_environment_released=released.virtual_environment_released,
            deployment_destroyed=released.deployment_destroyed,
        )
        return released

    async def _release_all_resources(
        self,
        *,
        model: EnvironmentModel,
        members: t.Sequence[str],
    ) -> ReleasedResources:
        """Release every resource backing an environment, in dependency order.

        The SQLMesh virtual environment goes first because it needs a working
        copy to reach the engine, and the working copies are what Git removal
        takes away. The Dagster deployment goes last: it is derived state that
        would be rebuilt FROM a working copy, and its teardown needs nothing
        but the frozen name. Shared by ``destroy`` and by the later cleanup of
        an archived environment, so both can never diverge.

        Takes no workspace: every resource released here is addressed by the
        environment row alone. The remote URL used to be needed for the branch
        deletion, which no longer exists (see ``destroy``).
        """
        virtual_env_released = await self._release_virtual_environment(
            tenant_id=model.tenant_id,
            workspace_id=model.workspace_id,
            environment=model.name,
        )
        worktrees_removed = await self._release_member_copies(
            tenant_id=model.tenant_id,
            workspace_id=model.workspace_id,
            environment=model.name,
            branch=model.branch,
            members=members,
        )
        deployment_destroyed = await self._release_deployment(model)

        return ReleasedResources(
            virtual_environment_released=virtual_env_released,
            worktrees_removed=worktrees_removed,
            deployment_destroyed=deployment_destroyed,
        )

    async def _release_deployment(self, model: EnvironmentModel) -> bool:
        """Stop the environment's Dagster deployment; name-only and best-effort.

        Never propagates: an unreachable runtime during a destroy is an
        honest partial outcome (reported as ``deployment_destroyed=False``)
        that the reconciler's ``stale_deployment`` repair finishes later.
        """
        if not model.deployment_name:
            return False
        return await self._deployment.destroy_deployment(
            deployment_name=model.deployment_name
        )

    async def _members_for_release(
        self, *, tenant_id: str, workspace_id: str, actor_user_id: str
    ) -> t.List[str]:
        """Members whose copies must be released, the actor first.

        Ordering matters for the operations that need ONE working copy (engine
        access, remote credentials): the actor is the copy whose credentials the
        request carries, so it is tried first, and other members are the
        fallback that keeps the cleanup complete when the actor never
        materialized this environment (or is not a member at all, as for the
        TTL reaper).
        """
        members = await self._git.list_member_user_ids(
            tenant_id=tenant_id, workspace_id=workspace_id
        )
        ordered = [actor_user_id] if actor_user_id in members else []
        ordered.extend(member for member in members if member != actor_user_id)
        return ordered

    async def _collect_pending_work(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        branch: str,
        members: t.Sequence[str],
    ) -> t.Dict[str, PendingWork]:
        """Probe every member's copy for work a release would destroy.

        EVERY member, because destroying an environment removes their worktrees
        too: another member's uncommitted work is exactly as unrecoverable as
        the caller's own (design §13). Members with nothing pending are left
        out, so the result reads as "who would lose something".
        """
        pending: t.Dict[str, PendingWork] = {}
        for member in members:
            work = await self._git.pending_work(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=member,
                environment=environment,
                branch=branch,
            )
            if work.uncommitted or work.unpushed:
                pending[member] = work
        return pending

    @staticmethod
    def _assert_no_uncommitted_work(
        pending: t.Mapping[str, PendingWork], environment: str, workspace_id: str
    ) -> None:
        """Refuse to discard changes that were never committed.

        Only UNCOMMITTED work gates a destroy. Unpushed commits are reported to
        the user before they confirm (the delete dialog reads the same probe)
        but never block: the platform would otherwise refuse every environment
        whose owner simply has not pushed yet, and the TTL reaper — which must
        always be able to reclaim — would be the first victim.
        """
        dirty = {
            member: work.uncommitted
            for member, work in pending.items()
            if work.uncommitted
        }
        if not dirty:
            return

        detail = ", ".join(f"{member}: {count}" for member, count in dirty.items())
        logger.info(
            "environment_destroy_blocked_by_uncommitted_work",
            workspace_id=workspace_id,
            environment=environment,
            members=list(dirty),
        )
        raise ConflictError(
            f"Environment {environment} has uncommitted changes in "
            f"{len(dirty)} working copy/copies ({detail}). Commit or stash "
            "them, or repeat the request with force=true to discard them."
        )

    async def _release_virtual_environment(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
    ) -> bool:
        """Invalidate an environment's SQLMesh views before Git resources go.

        The runtime picks any member's working copy (they all reach the same
        workspace-level state). When no copy exists on any replica the runtime
        answers 404 and the release is left to the reconciler (reported as
        ``False``) rather than silently reported as done.
        """
        released = await self._sqlmesh.invalidate_virtual_environment(
            # Tenant and workspace are all the runtime needs: it scans the
            # members for a usable working copy itself.
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            environment=environment,
        )
        if not released:
            logger.warning(
                "environment_virtual_env_release_skipped",
                workspace_id=workspace_id,
                environment=environment,
                reason="no_usable_working_copy",
            )
        return released

    async def _release_member_copies(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        branch: str,
        members: t.Sequence[str],
    ) -> int:
        """Remove every member's worktree and local branch of an environment.

        Each member is independent and best-effort: one broken copy must not
        leave the remaining members holding a branch that can resurrect the
        environment on their next push.
        """
        removed = 0
        for member in members:
            try:
                if await self._git.remove_worktree(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=member,
                    environment=environment,
                ):
                    removed += 1
                await self._git.delete_local_branch(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=member,
                    branch=branch,
                )
            except Exception as exc:  # noqa: BLE001 - keep releasing the rest
                logger.warning(
                    "environment_member_copy_release_failed",
                    workspace_id=workspace_id,
                    environment=environment,
                    user_id=member,
                    error=str(exc),
                )
        return removed

    async def _publish_branch(
        self,
        *,
        workspace: WorkspaceModel,
        worktree_path: str,
        branch: str,
        user_id: str,
    ) -> bool:
        """Push a new environment branch so other members can materialize it.

        Non-fatal on failure: the environment is usable locally by its creator,
        and the reconciler reports the missing remote branch.
        """
        if not workspace.git_repo_url:
            return False

        result = await self._git.push_branch(
            worktree_path=worktree_path,
            branch=branch,
            user_id=user_id,
            git_repo_url=workspace.git_repo_url,
        )
        if not result.ok:
            logger.warning(
                "environment_branch_publish_failed",
                workspace_id=workspace.id,
                branch=branch,
                reason=result.reason,
            )
        return result.ok

    async def _touch_workspace(self, workspace_id: str) -> None:
        """Best-effort last-active bookkeeping (never fails the request)."""
        try:
            await self._workspaces.update_last_active(workspace_id)
        except Exception as exc:
            logger.warning(
                "environment_touch_workspace_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )

    async def _record_event(self, environment_id: str, action: str, **kwargs: t.Any) -> None:
        """Append an audit event; never let auditing break the operation."""
        try:
            await self._repo.record_event(environment_id, action, **kwargs)
        except Exception as exc:
            logger.warning(
                "environment_event_record_failed",
                environment_id=environment_id,
                action=action,
                error=str(exc),
            )


def _expiry_for(env_type: str, ttl_hours: int) -> t.Tuple[t.Optional[datetime], int]:
    """Compute ``(expires_at, ttl_hours)`` for a new environment (UTC).

    Only sandboxes expire: shared environments are team assets whose lifetime is
    decided by the team, production never expires.
    """
    if env_type != naming.ENV_TYPE_SANDBOX:
        return None, 0

    ttl = ttl_hours or settings.sandbox_default_ttl_hours
    ttl = max(MIN_TTL_HOURS, min(int(ttl), MAX_TTL_HOURS))
    return datetime.now(timezone.utc) + timedelta(hours=ttl), ttl


def _elapsed_ms(started: float) -> int:
    """Milliseconds elapsed since a ``time.monotonic()`` mark."""
    return int((time.monotonic() - started) * 1000)


def _to_view(model: EnvironmentModel) -> EnvironmentView:
    """Map an ORM row onto the API read model."""
    return EnvironmentView(
        environment_id=model.id,
        workspace_id=model.workspace_id,
        tenant_id=model.tenant_id,
        name=model.name,
        branch=model.branch,
        env_type=model.env_type,
        base_env=model.base_env,
        owner_user_id=model.owner_user_id,
        status=model.status,
        deployment_name=model.deployment_name,
        schedules_enabled=bool(model.schedules_enabled),
        is_production=model.env_type == naming.ENV_TYPE_PRODUCTION,
        last_applied_commit=model.last_applied_commit or "",
        last_applied_at=model.last_applied_at,
        last_applied_dirty=bool(model.last_applied_dirty),
        last_promoted_to=model.last_promoted_to or "",
        ttl_hours=model.ttl_hours,
        expires_at=model.expires_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


# Global singleton: the registry is stateless beyond its binders.
_registry: t.Optional[EnvironmentRegistry] = None


def get_environment_registry() -> EnvironmentRegistry:
    """Get or create the global :class:`EnvironmentRegistry`."""
    global _registry
    if _registry is None:
        _registry = EnvironmentRegistry()
    return _registry
