"""Workspace lifecycle management.

A workspace is a TENANT-level shared development unit:

- **Database** (this service) is the single source of truth for its existence
  and metadata.
- **Git** is the only durable carrier of workspace code.
- **Disk** (workspace-runtime) holds disposable per-user material:
  ``{base}/{tenant_id}/{workspace_id}/{user_id}/.repo`` and
  ``.../{user_id}/envs/{environment}``. The control plane never touches it —
  every materialization, scaffold, sync and purge is dispatched over the
  env-control API.

Branches are not a workspace concern: every branch belongs to an
``Environment`` and is managed by :mod:`src.environments.registry`. This module
delegates all Git materialization to the registry so code and data views can
never drift apart.

Authorization is NOT performed here. The gateway owns the role bindings and is
the platform's single authorization center; by the time a request reaches this
module the caller's permissions have already been checked and are carried in
:class:`src.api.deps.CallerContext`. This module therefore only enforces tenant
scoping (a workspace of another tenant is reported as missing) and the domain
invariants that do not depend on roles.
"""

import typing as t
from dataclasses import dataclass
from datetime import datetime

from datacode_common import naming
from datacode_common import workspace_paths as ws_paths

from src.environments.registry import get_environment_registry
from src.operations import (
    KIND_CREATE,
    KIND_DELETE,
    OperationHandle,
    get_operation_recorder,
    steps,
)
from src.runtime.client import RuntimeTarget, get_runtime_client
from src.utils.errors import (
    ConflictError,
    EnvironmentNotFoundError,
    GitRepoNotBoundError,
    RuntimeUnavailableError,
    WorkspaceNotFoundError,
)
from src.utils.logging import get_logger
from src.workspace.models import WorkspaceModel

logger = get_logger(__name__)

# Reason codes returned to the frontend when a workspace cannot be opened.
BLOCK_REASON_NO_GIT_REPO = "no_git_repo"

# No remote was bound at all. Not a state the runtime can report (there is
# nothing for it to look at), so the control plane names it itself and the rest
# of ``create`` treats every case through one vocabulary.
REMOTE_STATE_LOCAL_BOOTSTRAP = "local_bootstrap"


@dataclass(frozen=True)
class RemoteProvision:
    """What the bound remote turned out to be, and what creation must do next.

    ``needs_scaffold`` / ``needs_bootstrap_commit`` are carried rather than
    re-derived from ``state``: the runtime decided them next to the evidence, and
    a second derivation here is a second place for "may I overwrite the user's
    code?" to be answered differently.
    """

    state: str
    normalized_url: str
    default_branch: str = ""
    created: bool = False
    needs_scaffold: bool = False
    needs_bootstrap_commit: bool = False


@dataclass(frozen=True)
class WorkspaceView:
    """Read model handed to the API layer.

    ``openable`` / ``open_blocked_reason`` are computed server-side on purpose:
    the frontend must not re-derive access rules from raw fields.

    ``role`` is echoed from the caller context for convenience; the gateway
    overwrites it (together with the effective permissions) in the response it
    returns to the client, so it stays the only authority on roles.
    """

    workspace_id: str
    tenant_id: str
    owner_user_id: str
    name: str
    dialect: str
    git_repo_url: str
    production_env: str
    default_env: str
    status: str
    role: str
    is_owner: bool
    openable: bool
    open_blocked_reason: str
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class OpenedWorkspace:
    """Result of materializing one environment working copy for one user."""

    workspace_id: str
    workspace_path: str
    environment: str
    branch: str
    head_commit: str


@dataclass(frozen=True)
class _LeaseRelease:
    """Outcome of releasing a workspace's leases before its files are removed.

    ``unknown`` separates "there were none" from "Redis could not be read": the
    second means a live writer may survive the deletion, which is a fact worth
    logging rather than rounding down to zero.
    """

    released: int
    unknown: bool
    # Addresses of the observed holders — the only evidence of which replica's
    # volume holds the working copy, and gone once the lease key is deleted.
    addrs: tuple


class WorkspaceManager:
    """Manages workspace lifecycle across DB (local) and Git/disk (runtime)."""

    def __init__(self) -> None:
        logger.info("workspace_manager_initialized")

    # ---- Public API: lifecycle ----

    async def create(
        self,
        tenant_id: str,
        owner_user_id: str,
        name: str,
        dialect: str = "duckdb",
        git_repo_url: str = "",
    ) -> WorkspaceView:
        """Create a workspace owned by ``owner_user_id`` inside ``tenant_id``.

        Registry-first ordering: the workspace row and its production
        environment are written before any disk work, so a failure never leaves
        a branch or worktree nobody accounts for.

        The bound Git URL is an INTENT, and the remote is resolved BEFORE
        anything is written (see ``provision_remote``). Its verdict decides the
        one question that must not be gotten wrong:

        * we created the repository, or none was bound at all -> generate the
          platform scaffold, which becomes the workspace's first commit;
        * the repository already carries code -> REUSE it verbatim. No scaffold:
          writing a template over a real project would corrupt it.

        Resolving first is also what makes failures legible. Previously the URL
        was only touched after the scaffold had been committed, so a
        non-existent repository surfaced as a failed fetch and then as an
        unexplained "add worktree" error.

        Durability: every step is recorded in ``workspace_operations``. The
        in-process rollback below is the fast path; if this process dies
        mid-way, :class:`src.operations.resumer.OperationResumer` finds the
        stalled record and finishes the rollback (or marks the operation done
        when only the final bookkeeping was lost).
        """
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(owner_user_id, field="user_id")
        slug = ws_paths.slugify(name)
        workspace_id = ws_paths.build_workspace_id(tenant_id, slug)

        # Tenant-level quota FIRST: a rejected request must leave nothing
        # behind, and this is the last point where nothing has been built.
        from src.quota import get_quota_service

        await get_quota_service().check_workspace_creation(tenant_id)

        client = get_runtime_client()
        repo = self._repository()
        if await repo.get_by_id(workspace_id) is not None:
            raise ConflictError(f"Workspace already exists: {workspace_id}")

        if git_repo_url and not git_repo_url.startswith(("http://", "https://", "git@", "ssh://")):
            raise ConflictError(
                "git_repo_url must be an HTTP(S) or SSH Git remote URL"
            )

        recorder = get_operation_recorder()
        op = await recorder.begin(
            kind=KIND_CREATE,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            actor_user_id=owner_user_id,
        )

        # Resolve the remote before the row is written: a rejected URL (no
        # credential, an organization the token may not create in, an
        # unreadable repository) must leave no workspace behind at all.
        try:
            provision = await self._provision_remote(
                client, git_repo_url=git_repo_url, user_id=owner_user_id
            )
        except Exception:
            await recorder.rolled_back(op, reason="remote_rejected")
            raise

        needs_push = bool(provision.normalized_url and provision.needs_scaffold)
        final_step = (
            steps.PUSHED
            if needs_push
            else steps.SCAFFOLDED if provision.needs_scaffold else steps.OPENED
        )
        # Recorded BEFORE the insert: a crash between the insert and the next
        # record must still leave a trace naming the row.
        await recorder.advance(op, steps.WRITING_ROW, final_step=final_step)

        model = WorkspaceModel(
            id=workspace_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            dialect=dialect,
            git_repo_url=provision.normalized_url or None,
            production_env=naming.PRODUCTION_ENV,
            default_env=naming.PRODUCTION_ENV,
            status="active",
        )
        try:
            await repo.create(model)
        except Exception:
            # A concurrent creation won the primary key; the row is not ours,
            # so there is nothing of ours to undo.
            await recorder.rolled_back(op, reason="row_not_written")
            raise
        await recorder.advance(op, steps.ROW_WRITTEN)

        registry = get_environment_registry()
        try:
            # ``open`` runs ``git/ensure-repository`` first, and that is the
            # step that PLACES the owner's production lease. Every call below
            # is addressed at the same copy, so the scaffold, its first commit
            # and the push all land on the replica that will serve the
            # workspace afterwards — not on whichever one DNS picked.
            production = await registry.ensure_production(model, owner_user_id)
            opened = await registry.open(
                environment_id=production.environment_id,
                tenant_id=tenant_id,
                user_id=owner_user_id,
                # An empty remote (or none at all) has no commit to check out,
                # and git cannot attach a worktree to an unborn branch.
                allow_bootstrap_commit=provision.needs_bootstrap_commit,
            )
            await recorder.advance(op, steps.OPENED)
            owner_copy = RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=owner_user_id,
                environment=opened.environment,
            )
            if provision.needs_scaffold:
                # Scaffold + initial commit run on the runtime (filesystem
                # access lives there, not here).
                await client.post(
                    "/internal/env-control/workspace/scaffold",
                    {
                        "workspace_path": opened.workspace_path,
                        "name": name,
                        "dialect": dialect,
                        "workspace_id": workspace_id,
                    },
                    target=owner_copy,
                )
                await recorder.advance(op, steps.SCAFFOLDED)
        except Exception as exc:
            # The DB is authoritative; drop the half-built workspace so a retry
            # starts from a clean slate. The remote is never deleted — it may be
            # a repository the user already owned, and even one we just created
            # is safer kept (the retry reuses it) than removed.
            complete = await self._rollback_creation(
                workspace_id, tenant_id, owner_user_id
            )
            if complete:
                await recorder.rolled_back(op, reason="creation_failed")
            else:
                # The resumer finishes what this rollback could not.
                await recorder.fail(op, error=f"rollback incomplete after: {exc}")
            logger.error(
                "workspace_create_rolled_back",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                remote_repository_created=provision.created,
                rollback_complete=complete,
            )
            raise

        if needs_push:
            # Only freshly scaffolded code is pushed. A reused repository is
            # already published, and pushing an unrelated first commit onto it
            # would either be rejected as non-fast-forward or, worse, land a
            # platform template on the user's branch.
            push = await client.post(
                "/internal/env-control/workspace/push-initial",
                {
                    "workspace_path": opened.workspace_path,
                    "git_repo_url": provision.normalized_url,
                    "branch": opened.branch,
                    "user_id": owner_user_id,
                },
                target=owner_copy,
            )
            await recorder.advance(op, steps.PUSHED, push_ok=bool(push.get("ok")))
            if not push.get("ok"):
                # Non-fatal: the workspace exists and the owner keeps a local
                # copy, but members cannot clone until the push succeeds.
                logger.error(
                    "workspace_initial_push_failed",
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    reason=push.get("reason"),
                )

        await recorder.succeed(op)

        logger.info(
            "workspace_created",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            dialect=dialect,
            has_git_remote=bool(provision.normalized_url),
            remote_state=provision.state,
            remote_repository_created=provision.created,
            scaffolded=provision.needs_scaffold,
        )
        return self._build_view(
            model,
            role="",
            requester_user_id=owner_user_id,
            owner_root_exists=True,
        )

    @staticmethod
    async def _provision_remote(
        client: t.Any, *, git_repo_url: str, user_id: str
    ) -> RemoteProvision:
        """Ask the runtime what the bound remote is, or describe the local case.

        Without a URL there is no remote to resolve, and the workspace enters the
        bootstrap window: the owner holds the only copy until they bind one. That
        case still needs a scaffold and a root commit, so it is expressed in the
        same vocabulary instead of as a second code path.
        """
        if not git_repo_url:
            return RemoteProvision(
                state=REMOTE_STATE_LOCAL_BOOTSTRAP,
                normalized_url="",
                needs_scaffold=True,
                needs_bootstrap_commit=True,
            )

        body = await client.post(
            "/internal/env-control/workspace/provision-remote",
            {"git_repo_url": git_repo_url, "user_id": user_id, "private": True},
        )
        return RemoteProvision(
            state=str(body.get("state", "")),
            normalized_url=str(body.get("normalized_url", git_repo_url)),
            default_branch=str(body.get("default_branch", "")),
            created=bool(body.get("created", False)),
            # Derived by the runtime: "may I scaffold?" is decided once, next to
            # the evidence, and never re-derived from the state string here.
            needs_scaffold=bool(body.get("needs_scaffold", False)),
            needs_bootstrap_commit=bool(body.get("needs_bootstrap_commit", False)),
        )

    async def list_for_ids(
        self, tenant_id: str, user_id: str, workspace_ids: t.Sequence[str]
    ) -> t.List[WorkspaceView]:
        """List the active workspaces named by ``workspace_ids``.

        The gateway resolved the identifiers from the caller's role bindings, so
        this is a metadata lookup restricted to the caller's tenant.
        """
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(user_id, field="user_id")

        repo = self._repository()
        models = await repo.list_by_ids(tenant_id, workspace_ids)

        # One membership probe per workspace answers "has this caller ever
        # opened it" for the bootstrap-openable computation below. It is a
        # union over replicas: the owner's copy lives on ONE volume, and asking
        # any other would report the owner as never having opened it.
        client = get_runtime_client()
        member_sets: t.Dict[str, t.Set[str]] = {}
        for model in models:
            if model.git_repo_url:
                continue  # bound remote -> openable regardless of a local copy
            try:
                replies = await client.get_all(
                    "/internal/env-control/git/members",
                    params={"tenant_id": tenant_id, "workspace_id": model.id},
                    target=RuntimeTarget(tenant_id=tenant_id, workspace_id=model.id),
                )
            except RuntimeUnavailableError:
                # Degraded listing beats a failed one; the UI re-derives on open.
                replies = []
            members: t.Set[str] = set()
            for reply in replies:
                if reply.body is not None:
                    members.update(str(m) for m in reply.body.get("members", []))
            member_sets[model.id] = members

        views: t.List[WorkspaceView] = []
        for model in models:
            views.append(
                self._build_view(
                    model,
                    role="",
                    requester_user_id=user_id,
                    owner_root_exists=user_id in member_sets.get(model.id, set()),
                )
            )

        logger.info(
            "workspaces_listed",
            tenant_id=tenant_id,
            user_id=user_id,
            count=len(views),
        )
        return views

    async def open(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        environment: str = "",
    ) -> OpenedWorkspace:
        """Materialize one environment working copy for the caller.

        Delegates to the environment registry: the environment decides the
        branch, and the branch is materialized as a Git worktree. Switching
        environment therefore never runs a ``checkout`` and is never blocked by
        uncommitted work in another environment.
        """
        model = await self._require_workspace(workspace_id, tenant_id)
        target_env = environment or model.default_env or naming.PRODUCTION_ENV

        if not model.git_repo_url and model.owner_user_id != user_id:
            # Bootstrap window: the owner still holds the only copy of the code,
            # so no other member can materialize anything yet.
            logger.warning(
                "workspace_open_blocked_no_git_repo",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            raise GitRepoNotBoundError(workspace_id)

        registry = get_environment_registry()
        opened = await registry.open(
            environment_id=naming.environment_id(workspace_id, target_env),
            tenant_id=tenant_id,
            user_id=user_id,
        )

        logger.info(
            "workspace_opened",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            environment=opened.environment,
            branch=opened.branch,
        )
        return OpenedWorkspace(
            workspace_id=workspace_id,
            workspace_path=opened.workspace_path,
            environment=opened.environment,
            branch=opened.branch,
            head_commit=opened.head_commit,
        )

    async def remote_branches(
        self, workspace_id: str, tenant_id: str, user_id: str
    ) -> t.List[str]:
        """List remote branches available for the workspace repository."""
        model = await self._require_workspace(workspace_id, tenant_id)
        if not model.git_repo_url:
            return []

        body = await get_runtime_client().get(
            "/internal/env-control/workspace/remote-branches",
            params={"git_repo_url": model.git_repo_url, "user_id": user_id},
        )
        branches = list(body.get("branches", []))
        logger.info(
            "workspace_remote_branches_listed",
            workspace_id=workspace_id,
            user_id=user_id,
            count=len(branches),
        )
        return branches

    async def bind_git_repo(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        git_repo_url: str,
    ) -> str:
        """Bind a remote repository to the workspace and push the current code.

        Binding decides where every member pulls the code from, so it is gated
        by the ``workspace:git:bind`` permission at the gateway.
        """
        model = await self._require_workspace(workspace_id, tenant_id)

        production_branch = naming.branch_of(model.production_env)
        local_path = self.environment_path(
            tenant_id, workspace_id, user_id, model.production_env
        )

        body = await get_runtime_client().post(
            "/internal/env-control/workspace/bind",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "worktree_path": local_path,
                "git_repo_url": git_repo_url,
                "production_branch": production_branch,
            },
            # The push reads the caller's production copy, so it must reach
            # the replica that holds it.
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=model.production_env,
            ),
        )
        normalized_url = str(body.get("normalized_url", git_repo_url))

        repo = self._repository()
        await repo.update_git_repo(workspace_id, normalized_url)
        logger.info(
            "workspace_git_bound",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return normalized_url

    async def sync_to_remote(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        message: str = "Auto-save",
        environment: str = "",
    ) -> bool:
        """Commit and push one environment working copy to the remote.

        Gated by the ``workspace:git:push`` permission at the gateway. The push
        always targets the environment's own branch, so a commit can never land
        on another environment's line of development.

        Rejected while an apply holds the environment: the commit would move
        HEAD underneath a running apply, and the applied data would then be
        attributed to code that was not the code it ran on.
        """
        model = await self._require_workspace(workspace_id, tenant_id)
        if not model.git_repo_url:
            raise GitRepoNotBoundError(workspace_id)

        target_env = environment or model.default_env or naming.PRODUCTION_ENV
        environment_model = await self._require_environment(workspace_id, target_env)
        local_path = self.environment_path(
            tenant_id, workspace_id, user_id, environment_model.name
        )

        body = await get_runtime_client().post(
            "/internal/env-control/workspace/sync",
            {
                "workspace_path": local_path,
                "branch": environment_model.branch,
                "message": message,
                "user_id": user_id,
                "git_repo_url": model.git_repo_url,
            },
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment_model.name,
            ),
        )

        logger.info(
            "workspace_synced",
            workspace_id=workspace_id,
            user_id=user_id,
            environment=environment_model.name,
            branch=environment_model.branch,
        )
        _ = body
        return True

    async def delete(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
    ) -> t.Dict[str, t.Any]:
        """Delete a workspace and everything the platform holds for it.

        Gated by the ``workspace:delete`` permission at the gateway, which also
        purges the role bindings once this call succeeds.

        Deletion is COMPLETE and PHYSICAL. It used to be a status flag plus a
        directory removal, and everything else survived to be inherited by the
        next workspace created under the same name: ghost knowledge-base entries
        whose files were gone, resurrected editor drafts, another workspace's
        apply history, an orphaned Dagster deployment, and a Redis lease still
        held by a live writer.

        Order is dependency-driven, and each step is independent so one failure
        cannot strand the rest:

        1. **leases** — released FIRST. While a lease is held, some runtime
           replica considers itself the single writer of that working copy; every
           later step operates on files it might still be writing;
        2. **environment resources** — SQLMesh virtual environments, working
           copies, Dagster deployments (never remote branches);
        3. **runtime purge** — broadcast to every replica that could hold a copy,
           because each has its own volume and none is authoritative for the
           others;
        4. **rows** — environments, then the workspace itself.

        The REMOTE GIT REPOSITORY IS NEVER TOUCHED, including its branches. It
        may predate the workspace, it may be shared, and deleting a user's Git
        repository as a side effect of removing a workspace is not a decision the
        platform may take.

        Audit rows (``environment_events``, ``lease_history``, ``task_records``)
        are kept on purpose: they now point at objects that no longer exist,
        which is what an audit trail is for.

        Every step is idempotent and recorded in ``workspace_operations``, so an
        interrupted deletion is driven to completion by the operation resumer
        (re-running :meth:`run_deletion`) rather than left half-done.
        """
        model = await self._require_workspace(workspace_id, tenant_id)
        op = await get_operation_recorder().begin(
            kind=KIND_DELETE,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            actor_user_id=user_id,
        )
        result = await self.run_deletion(
            workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id, op=op
        )
        logger.info(
            "workspace_deleted",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            had_git_remote=bool(model.git_repo_url),
            **{k: v for k, v in result.items() if k != "deleted"},
        )
        return result

    async def run_deletion(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        op: t.Optional[OperationHandle],
    ) -> t.Dict[str, t.Any]:
        """The deletion steps, in dependency order; safe to re-run from the top.

        Needs no workspace row: the resumer calls this after a crash that may
        have happened after the row was already deleted. Every step converges
        on "absent" — releasing a released lease, purging a purged replica and
        deleting a deleted row are all no-ops.
        """
        recorder = get_operation_recorder()
        registry = get_environment_registry()
        try:
            leases = await self._release_leases(tenant_id, workspace_id)
            await recorder.advance(op, steps.LEASES_RELEASED)
            released = await registry.release_all_for_workspace(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                actor_user_id=user_id,
            )
            await recorder.advance(op, steps.RESOURCES_RELEASED)
            purged_replicas = await self._broadcast_hard_purge(
                tenant_id=tenant_id, workspace_id=workspace_id, lease_addrs=leases.addrs
            )
            await recorder.advance(op, steps.REPLICAS_PURGED)
            environments_removed = await registry.purge_all_for_workspace(
                workspace_id=workspace_id
            )
            await self._repository().purge(workspace_id)
            await recorder.advance(op, steps.ROWS_DELETED)
        except Exception as exc:
            await recorder.fail(op, error=str(exc))
            raise

        await recorder.succeed(op)
        return {
            "deleted": True,
            "leases_released": leases.released,
            "environments_released": released.get("released", 0),
            "environments_removed": environments_removed,
            "replicas_purged": purged_replicas,
            # Stated explicitly in the response: the user ticked no box for it,
            # and the UI says so too.
            "git_remote_preserved": True,
        }

    @staticmethod
    async def _release_leases(tenant_id: str, workspace_id: str) -> "_LeaseRelease":
        """Release every lease of this workspace before its files are touched.

        A held lease means a runtime replica believes it is the single writer of
        that working copy. Removing the copy underneath it is what produces a
        writer operating on a deleted path — and, worse, a workspace later
        created under the same name inheriting a stale holder.

        Redis being unreadable is reported (``unknown``) rather than treated as
        "no leases": the deletion still proceeds, because the alternative is a
        workspace nobody can remove while Redis is down, but the ambiguity is
        logged instead of being silently rounded down to zero.
        """
        from src.environments.lease_binder import get_lease_binder

        binder = get_lease_binder()
        records = await binder.list_for_workspace(
            tenant_id=tenant_id, workspace_id=workspace_id
        )
        if records is None:
            logger.warning(
                "workspace_delete_leases_unknown",
                workspace_id=workspace_id,
                effect="proceeding; a live holder may survive the deletion",
            )
            return _LeaseRelease(released=0, unknown=True, addrs=())

        released = 0
        addrs: list[str] = []
        for record in records:
            # The holder's address is collected BEFORE the release: it is the
            # only evidence of which replica's volume holds this copy, and it is
            # gone once the key is deleted.
            if record.runtime_addr:
                addrs.append(record.runtime_addr)
            if await binder.release(record):
                released += 1

        logger.info(
            "workspace_leases_released",
            workspace_id=workspace_id,
            found=len(records),
            released=released,
        )
        return _LeaseRelease(
            released=released, unknown=False, addrs=tuple(dict.fromkeys(addrs))
        )

    @staticmethod
    async def _broadcast_hard_purge(
        *, tenant_id: str, workspace_id: str, lease_addrs: t.Sequence[str]
    ) -> int:
        """Purge on every replica that could hold a copy of this workspace.

        Each replica has its OWN volume and none is authoritative for the
        others, so the purge fans out to every live replica (see
        :mod:`src.runtime.routing`). The holders observed before the leases
        were released are added explicitly: a draining replica is excluded
        from the live fleet yet still holds files. The endpoint is idempotent,
        so overlapping recipients cost nothing.

        Returns the number of replicas that confirmed the purge.
        """
        replies = await get_runtime_client().post_all(
            "/internal/env-control/workspace/hard-purge",
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
            target=RuntimeTarget(tenant_id=tenant_id, workspace_id=workspace_id),
            also=lease_addrs,
        )
        for reply in replies:
            if not reply.ok:
                logger.warning(
                    "workspace_hard_purge_failed",
                    workspace_id=workspace_id,
                    target=reply.addr,
                    error=str(reply.error),
                )
        return sum(1 for reply in replies if reply.ok)

    @staticmethod
    async def _purge_member_everywhere(
        *, tenant_id: str, workspace_id: str, user_id: str
    ) -> bool:
        """Remove one member's local root on every replica; True if any had one.

        A member's environments may be spread over several volumes, and a root
        left on one of them keeps the code readable after access was revoked.
        """
        replies = await get_runtime_client().post_all(
            "/internal/env-control/workspace/purge-member",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
            target=RuntimeTarget(tenant_id=tenant_id, workspace_id=workspace_id),
        )
        failed = [reply.addr for reply in replies if not reply.ok]
        if failed:
            logger.warning(
                "workspace_member_purge_incomplete",
                workspace_id=workspace_id,
                member_user_id=user_id,
                unreachable_replicas=failed,
            )
        return any(
            bool(reply.body.get("purged", False))
            for reply in replies
            if reply.body is not None
        )

    # ---- Public API: working-copy housekeeping ----

    async def cleanup_member_copy(
        self, workspace_id: str, tenant_id: str, target_user_id: str
    ) -> bool:
        """Drop a revoked member's working copy.

        Access revocation itself happens in the gateway; this only removes the
        disposable checkout so the code stops being readable on disk. The owner's
        copy is never touched.
        """
        model = await self._require_workspace(workspace_id, tenant_id)
        ws_paths.validate_segment(target_user_id, field="target_user_id")

        if target_user_id == model.owner_user_id:
            raise ConflictError("The workspace owner's working copy cannot be removed")

        purged = await self._purge_member_everywhere(
            tenant_id=tenant_id, workspace_id=workspace_id, user_id=target_user_id
        )
        logger.info(
            "workspace_member_copy_removed",
            workspace_id=workspace_id,
            member_user_id=target_user_id,
        )
        return purged

    # ---- Public API: path derivation (pure computation for the wire) ----

    def user_root_path(self, tenant_id: str, workspace_id: str, user_id: str) -> str:
        """Derive a user's private root (``.repo`` + ``envs/``); never stored."""
        import os

        from src.config import settings

        return ws_paths.user_root_path(
            os.path.expanduser(settings.workspace_base_path),
            tenant_id,
            workspace_id,
            user_id,
        )

    def environment_path(
        self, tenant_id: str, workspace_id: str, user_id: str, environment: str
    ) -> str:
        """Derive one environment's working-copy path for one user."""
        import os

        from src.config import settings

        return ws_paths.environment_workspace_path(
            os.path.expanduser(settings.workspace_base_path),
            tenant_id,
            workspace_id,
            user_id,
            environment,
        )

    # ---- Private: lookup helpers ----

    @staticmethod
    def _repository() -> t.Any:
        """Instantiate the workspace repository (imported lazily)."""
        from src.workspace.repository import WorkspaceRepository

        return WorkspaceRepository()

    async def _require_workspace(
        self, workspace_id: str, tenant_id: str
    ) -> WorkspaceModel:
        """Resolve a workspace inside a tenant, or raise 404.

        A workspace of another tenant yields the same 404 as a non-existent one,
        so identifiers cannot be enumerated across tenants. Whether the caller
        may use it was already decided by the gateway.
        """
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(workspace_id, field="workspace_id")

        repo = self._repository()
        model = await repo.get_in_tenant(workspace_id, tenant_id)
        if model is None:
            logger.warning(
                "workspace_not_found",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
            )
            raise WorkspaceNotFoundError(workspace_id)
        return model

    async def _require_environment(self, workspace_id: str, environment: str) -> t.Any:
        """Resolve a registered environment of a workspace, or raise 404.

        An unregistered environment name is never usable: the registry is the
        source of truth, so a bare Git branch cannot be operated on.
        """
        from src.environments.repository import EnvironmentRepository

        model = await EnvironmentRepository().get_by_name(workspace_id, environment)
        if model is None:
            logger.warning(
                "workspace_environment_not_registered",
                workspace_id=workspace_id,
                environment=environment,
            )
            raise EnvironmentNotFoundError(
                naming.environment_id(workspace_id, environment)
            )
        return model

    # ---- Private: view assembly ----

    def _build_view(
        self,
        model: WorkspaceModel,
        role: str,
        requester_user_id: str,
        owner_root_exists: bool,
    ) -> WorkspaceView:
        """Assemble the API read model, computing openability server-side."""
        is_owner = model.owner_user_id == requester_user_id
        git_repo_url = model.git_repo_url or ""
        openable = bool(git_repo_url)
        blocked_reason = ""

        if not openable:
            # Owner-only bootstrap: the freshly scaffolded copy is still local.
            if is_owner and owner_root_exists:
                openable = True
            else:
                blocked_reason = BLOCK_REASON_NO_GIT_REPO

        return WorkspaceView(
            workspace_id=model.id,
            tenant_id=model.tenant_id,
            owner_user_id=model.owner_user_id,
            name=model.name,
            dialect=model.dialect,
            git_repo_url=git_repo_url,
            production_env=model.production_env,
            default_env=model.default_env,
            status=model.status,
            role=role,
            is_owner=is_owner,
            openable=openable,
            open_blocked_reason=blocked_reason,
            created_at=model.created_at,
            last_active_at=model.last_active_at,
        )

    # ---- Private: rollback ----

    async def _rollback_creation(
        self, workspace_id: str, tenant_id: str, owner_user_id: str
    ) -> bool:
        """Undo a failed workspace creation; True when every step completed.

        Order: the owner's material on every replica, the lease, the
        environment rows, and the workspace row LAST — the row is what lets
        the operation resumer (and a human) still find the leftovers if this
        rollback is interrupted.

        The row is PHYSICALLY removed, not marked deleted. A tombstone would be
        invisible to the duplicate check (which ignores deleted rows) while still
        occupying the primary key — the id is derived from ``{tenant}/{name}`` —
        so every retry of the very same creation would fail on a key conflict
        instead of starting clean.

        Each step is attempted independently: the goal is that a retry finds
        nothing left over. A step that fails is reported (False) so the caller
        can hand the remainder to the resumer. The remote is never touched.
        """
        complete = True

        try:
            await self._purge_member_everywhere(
                tenant_id=tenant_id, workspace_id=workspace_id, user_id=owner_user_id
            )
        except Exception as e:  # noqa: BLE001 - rollback must not raise
            complete = False
            logger.warning(
                "workspace_rollback_runtime_failed",
                workspace_id=workspace_id,
                error=str(e),
            )

        try:
            leases = await self._release_leases(tenant_id, workspace_id)
            complete = complete and not leases.unknown
        except Exception as e:  # noqa: BLE001
            complete = False
            logger.warning(
                "workspace_rollback_lease_failed", workspace_id=workspace_id, error=str(e)
            )

        try:
            await get_environment_registry().purge_all_for_workspace(
                workspace_id=workspace_id
            )
            await self._repository().purge(workspace_id)
        except Exception as e:  # noqa: BLE001
            complete = False
            logger.warning(
                "workspace_rollback_db_failed", workspace_id=workspace_id, error=str(e)
            )

        return complete

    async def rollback_creation(
        self, *, workspace_id: str, tenant_id: str, owner_user_id: str
    ) -> bool:
        """Public entry for the operation resumer (same semantics)."""
        return await self._rollback_creation(workspace_id, tenant_id, owner_user_id)


# Global singleton
_manager: t.Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    """Get or create the global WorkspaceManager instance."""
    global _manager
    if _manager is None:
        _manager = WorkspaceManager()
    return _manager
