"""Environment-control endpoints — control-service's command channel (design 06 P5).

These are INTERNAL endpoints (shared-secret guarded, never user traffic). The
control plane owns the registry and the state machine, but it must have NO
filesystem access; every operation that touches Git, the working copies or the
SQLMesh engine is dispatched here and executed by the runtime replica that
receives it.

Endpoint families
-----------------
``/internal/env-control/git/*``      branch & worktree lifecycle (GitBinder)
``/internal/env-control/sqlmesh/*``  virtual environments (SqlMeshBinder)
``/internal/env-control/workspace/*`` workspace provisioning & teardown
``/internal/env-control/sandbox/*``  user-code Pod inventory & reclaim (P8)

Mapping to the legacy binder semantics (control-service's HTTP clients):
- binder methods that took a filesystem-derived path (``repo_path``,
  ``worktree_path``) are replaced by logical identifiers (``tenant_id``,
  ``workspace_id``); the runtime derives the paths itself, so the control plane
  never has to know where anything lives on disk.
- the SQLMesh endpoints derive the working copy by scanning the members, exactly
  like the legacy ``first_existing_worktree`` logic.
"""

from __future__ import annotations

import asyncio
import os

from datacode_common import workspace_paths as ws_paths
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.api.dagster.internal_auth import InternalCaller
from src.environments.git_binder import GitBinder
from src.environments.sqlmesh_binder import get_sqlmesh_binder
from src.utils.errors import ErrorCode, ServiceError
from src.utils.logging import get_logger
from src.workspace.scaffold import get_workspace_provisioner

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/env-control", tags=["internal-env-control"])


def _git_binder() -> GitBinder:
    """The runtime's Git binder, bound to the configured workspace base path."""
    from src.config import settings

    return GitBinder(os.path.expanduser(settings.workspace_base_path))


def _validate_ids(tenant_id: str, workspace_id: str, *user_ids: str) -> None:
    """Reject path-shaped identifiers before they reach the filesystem.

    Internal callers are trusted, but a misconfigured control plane must not be
    able to make the runtime walk outside the workspace layout.
    """
    try:
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(workspace_id, field="workspace_id")
        for user_id in user_ids:
            if user_id:
                ws_paths.validate_segment(user_id, field="user_id")
    except ws_paths.WorkspacePathLayoutError as exc:
        # Same mapping the public API applies (see main.py): layout violations
        # are caller mistakes, never server faults.
        raise ServiceError(
            ErrorCode.WORKSPACE_PATH_INVALID, str(exc), status_code=400
        ) from exc


def _validate_user_id(user_id: str) -> None:
    """Validate a lone user id (endpoints that derive no workspace path).

    Separate from :func:`_validate_ids`, whose first two positional parameters
    are the tenant and workspace: calling it with a single argument raises a
    TypeError at request time, turning a validation step into a 500.
    """
    try:
        ws_paths.validate_segment(user_id, field="user_id")
    except ws_paths.WorkspacePathLayoutError as exc:
        raise ServiceError(
            ErrorCode.WORKSPACE_PATH_INVALID, str(exc), status_code=400
        ) from exc


# ---------------------------------------------------------------------------
# Git: repository & worktree lifecycle
# ---------------------------------------------------------------------------


class EnsureRepositoryRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    git_repo_url: str = ""
    bootstrap_branch: str = ""
    fetch: bool = True
    # Seed an empty root commit even though a remote IS configured. Only the
    # caller knows the remote is legitimately branchless (a repository it just
    # created, or one the user chose to reuse while empty); guessing here would
    # mean seeding a root commit into a repository whose refs simply failed to
    # fetch, forking its history.
    allow_bootstrap_commit: bool = False


@router.post("/git/ensure-repository")
async def ensure_repository(
    req: EnsureRepositoryRequest, _internal: InternalCaller
) -> dict:
    """Ensure the user's bare repository exists and mirrors the remote."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    repo_path = await _git_binder().ensure_repository(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        git_repo_url=req.git_repo_url,
        bootstrap_branch=req.bootstrap_branch,
        fetch=req.fetch,
        allow_bootstrap_commit=req.allow_bootstrap_commit,
    )
    return {"repo_path": repo_path}


class EnsureWorktreeRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    branch: str = Field(min_length=1, max_length=255)
    start_branch: str = ""


@router.post("/git/ensure-worktree")
async def ensure_worktree(
    req: EnsureWorktreeRequest, _internal: InternalCaller
) -> dict:
    """Ensure ``environment``'s worktree exists for ``user_id`` (idempotent)."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    materialization = await _git_binder().ensure_worktree(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        environment=req.environment,
        branch=req.branch,
        start_branch=req.start_branch,
    )
    return {
        "workspace_path": materialization.workspace_path,
        "branch": materialization.branch,
        "commit": materialization.commit,
        "created": materialization.created,
        "branch_source": materialization.branch_source,
    }


@router.get("/git/members")
async def list_members(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    _internal: InternalCaller = None,
) -> dict:
    """User ids holding a local root of the workspace (disk scan)."""
    _validate_ids(tenant_id, workspace_id)
    members = await _git_binder().list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    return {"members": members}


@router.get("/git/branches")
async def list_environment_branches(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    _internal: InternalCaller = None,
) -> dict:
    """Environment names backed by a branch in the platform namespace.

    Combines the legacy member-scan + first-repo + branch-list sequence into
    one round trip.
    """
    _validate_ids(tenant_id, workspace_id)
    binder = _git_binder()
    members = await binder.list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    repo = binder.first_existing_repo(
        tenant_id=tenant_id, workspace_id=workspace_id, members=members
    )
    environments = await binder.list_environment_branches(repo_path=repo)
    return {"environments": sorted(environments), "repo_found": bool(repo)}


@router.get("/git/branch-presence")
async def branch_presence(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    branch: str = Query(min_length=1, max_length=255),
    _internal: InternalCaller = None,
) -> dict:
    """(local, remote) existence of a branch, read from any member's clone."""
    _validate_ids(tenant_id, workspace_id)
    binder = _git_binder()
    members = await binder.list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    repo = binder.first_existing_repo(
        tenant_id=tenant_id, workspace_id=workspace_id, members=members
    )
    local, remote = await binder.branch_presence(repo_path=repo, branch=branch)
    return {"local": local, "remote": remote}


@router.get("/git/worktrees")
async def list_environment_worktrees(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    user_id: str = Query(min_length=1, max_length=128),
    _internal: InternalCaller = None,
) -> dict:
    """Map ``environment -> branch`` for the worktrees present on disk."""
    _validate_ids(tenant_id, workspace_id, user_id)
    worktrees = await _git_binder().list_environment_worktrees(
        tenant_id=tenant_id, workspace_id=workspace_id, user_id=user_id
    )
    return {"worktrees": worktrees}


@router.get("/git/pending-work")
async def pending_work(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    user_id: str = Query(min_length=1, max_length=128),
    environment: str = Query(min_length=1, max_length=128),
    branch: str = Query(min_length=1, max_length=255),
    _internal: InternalCaller = None,
) -> dict:
    """Work one member's working copy would lose if it were released.

    Both counts in one round trip: the caller asks per member, and splitting
    them would double the number of calls for two answers derived from the
    same working copy.
    """
    _validate_ids(tenant_id, workspace_id, user_id)
    work = await _git_binder().pending_work(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        environment=environment,
        branch=branch,
    )
    return {"uncommitted": work.uncommitted, "unpushed": work.unpushed}


@router.get("/git/first-existing")
async def first_existing(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    environment: str = Query(default="", max_length=128),
    _internal: InternalCaller = None,
) -> dict:
    """First member's bare repo (and, with ``environment``, working copy) on disk.

    Backs the legacy binder's ``first_existing_repo`` / ``first_existing_worktree``
    helpers: read-only probes whose answers are filesystem facts, so an empty
    string means "not materialized here", never an error.
    """
    _validate_ids(tenant_id, workspace_id)
    binder = _git_binder()
    members = await binder.list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    repo = binder.first_existing_repo(
        tenant_id=tenant_id, workspace_id=workspace_id, members=members
    )
    worktree = ""
    if environment:
        worktree = binder.first_existing_worktree(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            environment=environment,
            members=members,
        )
    return {"repo_path": repo, "worktree_path": worktree}


@router.get("/git/worktree-head")
async def worktree_head(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    environment: str = Query(min_length=1, max_length=128),
    _internal: InternalCaller = None,
) -> dict:
    """HEAD commit of the environment's first materialized working copy.

    Powers the commit-drift check ("branch moved, data did not"): an empty
    commit means no working copy exists, which is NOT drift.
    """
    _validate_ids(tenant_id, workspace_id)
    binder = _git_binder()
    members = await binder.list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    worktree = binder.first_existing_worktree(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        environment=environment,
        members=members,
    )
    if not worktree:
        return {"commit": ""}

    from src.git.ops import GitWorktreeOperations

    commit = await asyncio.to_thread(GitWorktreeOperations.head_commit, worktree)
    return {"commit": commit}


class RemoveWorktreeRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)


@router.post("/git/remove-worktree")
async def remove_worktree(
    req: RemoveWorktreeRequest, _internal: InternalCaller
) -> dict:
    """Remove one user's worktree of an environment (branch is kept)."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    removed = await _git_binder().remove_worktree(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        environment=req.environment,
    )
    return {"removed": removed}


class DeleteBranchRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    branch: str = Field(min_length=1, max_length=255)


@router.post("/git/delete-branch")
async def delete_local_branch(
    req: DeleteBranchRequest, _internal: InternalCaller
) -> dict:
    """Delete the local branch backing an environment."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    ok = await _git_binder().delete_local_branch(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        branch=req.branch,
    )
    return {"ok": ok}


class PushBranchRequest(BaseModel):
    worktree_path: str = Field(min_length=1, max_length=1024)
    branch: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    git_repo_url: str = Field(min_length=1, max_length=2048)


@router.post("/git/push-branch")
async def push_branch(req: PushBranchRequest, _internal: InternalCaller) -> dict:
    """Publish an environment branch so the whole team can materialize it."""
    result = await _git_binder().push_branch(
        worktree_path=req.worktree_path,
        branch=req.branch,
        user_id=req.user_id,
        git_repo_url=req.git_repo_url,
    )
    return {"ok": result.ok, "reason": result.reason, "detail": result.detail}


# There is deliberately no "delete remote branch" endpoint. Environment
# destruction releases local state only; the remote branch survives and is what
# a same-named environment reuses later.


# ---------------------------------------------------------------------------
# SQLMesh: virtual environments
# ---------------------------------------------------------------------------


class EnsureVirtualEnvironmentRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    environment: str = Field(min_length=1, max_length=128)
    base_env: str = Field(min_length=1, max_length=128)
    gateway: str = ""


@router.post("/sqlmesh/ensure-virtual-environment")
async def ensure_virtual_environment(
    req: EnsureVirtualEnvironmentRequest, _internal: InternalCaller
) -> dict:
    """Create ``environment`` as a zero-cost virtual copy of ``base_env``."""
    _validate_ids(req.tenant_id, req.workspace_id)
    workspace_path = await _first_existing_worktree(
        req.tenant_id, req.workspace_id, req.environment
    )
    if not workspace_path:
        raise ServiceError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            "no member working copy is materialized for this environment; "
            "open the workspace before creating environments",
            status_code=404,
        )
    ok = await get_sqlmesh_binder().ensure_virtual_environment(
        workspace_path=workspace_path,
        environment=req.environment,
        base_env=req.base_env,
        gateway=req.gateway or None,
    )
    return {"ok": ok}


class InvalidateVirtualEnvironmentRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    environment: str = Field(min_length=1, max_length=128)
    gateway: str = ""


@router.post("/sqlmesh/invalidate-virtual-environment")
async def invalidate_virtual_environment(
    req: InvalidateVirtualEnvironmentRequest, _internal: InternalCaller
) -> dict:
    """Invalidate an environment and drop its expired views."""
    _validate_ids(req.tenant_id, req.workspace_id)
    workspace_path = await _first_existing_worktree(
        req.tenant_id, req.workspace_id, req.environment
    )
    if not workspace_path:
        raise ServiceError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            "no member working copy is materialized for this environment; "
            "the environment state cannot be reached",
            status_code=404,
        )
    ok = await get_sqlmesh_binder().invalidate_virtual_environment(
        workspace_path=workspace_path,
        environment=req.environment,
        gateway=req.gateway or None,
    )
    return {"ok": ok}


@router.get("/sqlmesh/state-environments")
async def list_state_environments(
    tenant_id: str = Query(min_length=1, max_length=128),
    workspace_id: str = Query(min_length=1, max_length=255),
    environment: str = Query(default="", max_length=128),
    gateway: str = Query(default="", max_length=128),
    _internal: InternalCaller = None,
) -> dict:
    """Names of the environments that ACTUALLY exist in the SQLMesh state.

    ``environments`` is ``null`` (not an empty list) when the state could not be
    read at all — the distinction keeps the reconciler honest (an unreachable
    warehouse must not be treated as "no environments exist").
    """
    _validate_ids(tenant_id, workspace_id)
    workspace_path = await _first_existing_worktree(
        tenant_id, workspace_id, environment
    )
    if not workspace_path:
        return {"environments": None, "reason": "no_materialized_worktree"}
    environments = await get_sqlmesh_binder().list_state_environments(
        workspace_path=workspace_path, gateway=gateway or None
    )
    return {"environments": sorted(environments) if environments is not None else None}


async def _first_existing_worktree(
    tenant_id: str, workspace_id: str, environment: str
) -> str:
    """Locate any member's working copy of ``environment`` (engine access).

    Mirrors the legacy ``GitBinder.first_existing_worktree``: every member's copy
    declares the same workspace-level SQLMesh state, so any of them will do.
    """
    binder = _git_binder()
    members = await binder.list_member_user_ids(
        tenant_id=tenant_id, workspace_id=workspace_id
    )
    return binder.first_existing_worktree(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        environment=environment,
        members=members,
    )


# ---------------------------------------------------------------------------
# Sandbox inventory — the reconciler's sandbox party (design 05 §5.1)
# ---------------------------------------------------------------------------
#
# The control plane owns the registry but has no cluster client, so it cannot
# see which user-code Pods exist. It asks here, compares the answer against its
# own rows, and sends back only the deletions its comparison justified. The
# runtime never decides what is an orphan — it reports and it reclaims.


@router.get("/sandbox/inventory")
async def sandbox_inventory(_internal: InternalCaller) -> dict:
    """Code-location and task Pods this cell is running.

    ``null`` (not ``[]``) means the cluster could not be read, which the
    reconciler must treat as "no verdict" — the same honesty rule the SQLMesh
    and deployment parties follow. ``available: false`` is different and benign:
    this deployment has no container plane, so there is no sandbox party at all.
    """
    from src.tasks.inventory import get_sandbox_inventory

    inventory = get_sandbox_inventory()
    if not inventory.available:
        return {"available": False, "code_location_pods": None, "task_pods": None}

    code_location_pods = await inventory.list_code_location_pods()
    task_pods = await inventory.list_task_pods()
    return {
        "available": True,
        "code_location_pods": (
            [pod.as_dict() for pod in code_location_pods]
            if code_location_pods is not None
            else None
        ),
        "task_pods": (
            [pod.as_dict() for pod in task_pods] if task_pods is not None else None
        ),
    }


class DeleteCodeLocationPodRequest(BaseModel):
    pod_name: str = Field(min_length=1, max_length=253)
    # Required, not optional: it is the second coincidence an accidental match
    # would have to satisfy (see SandboxInventory.delete_code_location_pod).
    environment: str = Field(min_length=1, max_length=128)


@router.post("/sandbox/delete-code-location-pod")
async def delete_code_location_pod(
    req: DeleteCodeLocationPodRequest, _internal: InternalCaller
) -> dict:
    """Delete one orphaned code-location Pod (and its Service).

    Re-verifies the Pod's labels against ``environment`` before deleting: the
    reconciler's decision may be a pass old, and a Pod recreated meanwhile for a
    live environment must survive.
    """
    from src.tasks.inventory import get_sandbox_inventory

    deleted = await get_sandbox_inventory().delete_code_location_pod(
        pod_name=req.pod_name, environment=req.environment
    )
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Workspace provisioning & teardown
# ---------------------------------------------------------------------------


class ScaffoldRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)
    dialect: str = "duckdb"
    workspace_id: str = Field(min_length=1, max_length=255)


@router.post("/workspace/scaffold")
async def scaffold_workspace(req: ScaffoldRequest, _internal: InternalCaller) -> dict:
    """Scaffold the sql-warehouse + asset-dag layout and commit it."""
    await get_workspace_provisioner().scaffold_and_commit(
        workspace_path=req.workspace_path,
        name=req.name,
        dialect=req.dialect,
        workspace_id=req.workspace_id,
    )
    return {"scaffolded": True}


class PushInitialRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=1024)
    git_repo_url: str = Field(min_length=1, max_length=2048)
    branch: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)


@router.post("/workspace/push-initial")
async def push_initial_code(req: PushInitialRequest, _internal: InternalCaller) -> dict:
    """Point the freshly scaffolded copy at the remote and push it."""
    result = await get_workspace_provisioner().push_initial_code(
        local_path=req.workspace_path,
        git_repo_url=req.git_repo_url,
        branch=req.branch,
        user_id=req.user_id,
    )
    return {"ok": result.ok, "reason": result.reason, "detail": result.detail}


class BindRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)
    worktree_path: str = Field(min_length=1, max_length=1024)
    git_repo_url: str = Field(min_length=1, max_length=2048)
    production_branch: str = Field(min_length=1, max_length=255)


@router.post("/workspace/bind")
async def bind_repository(req: BindRequest, _internal: InternalCaller) -> dict:
    """Bind a remote repository: validate URL, push code, fetch into bare repo."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    normalized = await get_workspace_provisioner().bind_repository(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        worktree_path=req.worktree_path,
        git_repo_url=req.git_repo_url,
        production_branch=req.production_branch,
    )
    return {"normalized_url": normalized}


class SyncRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=1024)
    branch: str = Field(min_length=1, max_length=255)
    message: str = "Auto-save"
    user_id: str = Field(min_length=1, max_length=128)
    git_repo_url: str = Field(min_length=1, max_length=2048)


@router.post("/workspace/sync")
async def sync_to_remote(req: SyncRequest, _internal: InternalCaller) -> dict:
    """Commit and push one environment working copy to the remote."""
    synced = await get_workspace_provisioner().sync_to_remote(
        workspace_path=req.workspace_path,
        branch=req.branch,
        message=req.message,
        user_id=req.user_id,
        git_repo_url=req.git_repo_url,
    )
    return {"synced": synced}


@router.get("/workspace/remote-branches")
async def remote_branches(
    git_repo_url: str = Query(min_length=1, max_length=2048),
    user_id: str = Query(min_length=1, max_length=128),
    _internal: InternalCaller = None,
) -> dict:
    """List the remote's branches (for the workspace-open branch picker)."""
    branches = await get_workspace_provisioner().remote_branches(
        git_repo_url, user_id
    )
    return {"branches": branches}


class ProvisionRemoteRequest(BaseModel):
    git_repo_url: str = Field(min_length=1, max_length=2048)
    user_id: str = Field(min_length=1, max_length=128)
    # Repositories the platform creates are private. Exposed as a field rather
    # than hardcoded so the decision stays visible on the wire.
    private: bool = True


@router.post("/workspace/provision-remote")
async def provision_remote(
    req: ProvisionRemoteRequest, _internal: InternalCaller
) -> dict:
    """Resolve the bound remote into a creation path (create / reuse / empty).

    Runs BEFORE anything is written, because the answer decides whether the
    platform scaffold may be generated at all: a repository that already carries
    code must never be overwritten by a template.

    No workspace identity is needed or accepted: this only inspects a remote, so
    ``user_id`` (whose credential authenticates) is the sole identifier — and it
    is validated on its own, because :func:`_validate_ids` is shaped for the
    path-deriving endpoints.
    """
    _validate_user_id(req.user_id)
    provision = await get_workspace_provisioner().provision_remote(
        git_repo_url=req.git_repo_url,
        user_id=req.user_id,
        private=req.private,
    )
    return {
        "state": provision.state.value,
        "normalized_url": provision.normalized_url,
        "default_branch": provision.default_branch,
        "created": provision.created,
        # Derived server-side so the control plane never re-derives "may I
        # scaffold?" from the state string.
        "needs_scaffold": provision.needs_scaffold,
        "needs_bootstrap_commit": provision.needs_bootstrap_commit,
    }


class HardPurgeRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)


@router.post("/workspace/hard-purge")
async def hard_purge_workspace(
    req: HardPurgeRequest, _internal: InternalCaller
) -> dict:
    """Erase every trace of a workspace this service holds.

    Deliberately has NO ``delete_remote`` field, and the capability behind it no
    longer exists: deleting a Git repository is not something the platform may do
    as a side effect of removing a workspace. The repository may predate the
    workspace and outlives it by design.

    Idempotent, so the control plane can broadcast it to every replica that might
    hold a copy (each has its own volume) and retry without consequence.
    """
    _validate_ids(req.tenant_id, req.workspace_id)
    return await get_workspace_provisioner().hard_purge_workspace(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
    )


class PurgeMemberRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=128)


@router.post("/workspace/purge-member")
async def purge_member_copy(req: PurgeMemberRequest, _internal: InternalCaller) -> dict:
    """Drop one member's working copy (access revocation housekeeping)."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    purged = await get_workspace_provisioner().purge_member_copy(
        tenant_id=req.tenant_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
    )
    return {"purged": purged}


class ReleaseContextsRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=255)
    user_id: str = ""


@router.post("/workspace/release-contexts")
async def release_cached_contexts(
    req: ReleaseContextsRequest, _internal: InternalCaller
) -> dict:
    """Evict pooled SQLMesh contexts of a workspace (or of one member)."""
    _validate_ids(req.tenant_id, req.workspace_id, req.user_id)
    await get_workspace_provisioner().release_cached_contexts(
        req.tenant_id, req.workspace_id, req.user_id
    )
    return {"released": True}
