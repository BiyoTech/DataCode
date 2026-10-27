"""Workspace management endpoints.

A workspace is a tenant-level shared unit. Identity and authorization come
exclusively from the gateway through headers it fully controls (see
``src.api.deps.CallerContext``): the gateway resolves the caller's roles from the
RBAC tables, decides the request and forwards the outcome. Request bodies and
query strings are therefore never an authorization source here.

No local filesystem path is ever accepted or returned by the list endpoint — the
working copy path is only produced by ``POST /{workspace_id}/open``.
"""

import typing as t

from datacode_common.rbac import (
    PERM_TENANT_WORKSPACE_CREATE,
    PERM_WORKSPACE_DELETE,
    PERM_WORKSPACE_GIT_BIND,
    PERM_WORKSPACE_GIT_PUSH,
    PERM_WORKSPACE_MEMBER_WRITE,
    PERM_WORKSPACE_READ,
)
from fastapi import APIRouter, Depends, Query

from src.api.deps import Caller, CallerContext, require_permission
from src.api.models.requests import (
    BindGitRepoRequest,
    CreateWorkspaceRequest,
    OpenWorkspaceRequest,
    SyncWorkspaceRequest,
)
from src.utils.logging import get_logger
from src.workspace.manager import WorkspaceView, get_workspace_manager

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspace"])

# Permission codes come from the shared catalog (shared/rbac-catalog.json), the
# platform's single declaration of which permission points exist. They are
# asserted here as defence in depth: the gateway already denied the request when
# they are absent.


def _serialize(ws: WorkspaceView) -> t.Dict[str, t.Any]:
    """Serialize a workspace read model for the gateway.

    ``openable`` / ``open_blocked_reason`` are authoritative: the UI must not
    re-derive them from ``git_repo_url``. ``role`` and ``permissions`` are filled
    in by the gateway, which owns the role bindings.
    """
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "dialect": ws.dialect,
        "git_repo_url": ws.git_repo_url,
        "production_env": ws.production_env,
        "default_env": ws.default_env,
        "status": ws.status,
        "role": ws.role,
        "is_owner": ws.is_owner,
        "openable": ws.openable,
        "open_blocked_reason": ws.open_blocked_reason,
        "created_at": ws.created_at.isoformat(),
        "last_active_at": ws.last_active_at.isoformat(),
    }


@router.post("")
async def create_workspace(
    req: CreateWorkspaceRequest,
    caller: CallerContext = Depends(require_permission(PERM_TENANT_WORKSPACE_CREATE)),
):
    """Create a workspace owned by the caller inside their tenant."""
    ws = await get_workspace_manager().create(
        tenant_id=caller.tenant_id,
        owner_user_id=caller.user_id,
        name=req.name,
        dialect=req.dialect,
        git_repo_url=req.git_repo_url,
    )
    return _serialize(ws)


@router.get("")
async def list_workspaces(
    caller: Caller,
    workspace_ids: str = Query(
        default="",
        description="Comma-separated workspace ids the caller is allowed to see.",
    ),
):
    """List the workspaces named by ``workspace_ids`` inside the caller's tenant.

    The gateway resolved the identifiers from the caller's role bindings, so an
    empty list is a legitimate answer and never a lookup over all workspaces.
    """
    ids = [value.strip() for value in workspace_ids.split(",") if value.strip()]
    if not ids:
        return {"workspaces": []}

    workspaces = await get_workspace_manager().list_for_ids(
        caller.tenant_id, caller.user_id, ids
    )
    return {"workspaces": [_serialize(ws) for ws in workspaces]}


@router.post("/{workspace_id}/open")
async def open_workspace(
    workspace_id: str,
    req: OpenWorkspaceRequest,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_READ)),
):
    """Materialize the caller's working copy of one environment.

    The environment (not a branch) decides which code is materialized; an empty
    request falls back to the workspace's ``default_env``. Returns 409
    ``GIT_REPO_NOT_BOUND`` when a non-owner opens a workspace whose code has not
    been pushed yet.
    """
    opened = await get_workspace_manager().open(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
        environment=req.environment,
    )
    return {
        "workspace_id": opened.workspace_id,
        "workspace_path": opened.workspace_path,
        "environment": opened.environment,
        "branch": opened.branch,
        "head_commit": opened.head_commit,
    }


@router.get("/{workspace_id}/remote-branches")
async def list_remote_branches(
    workspace_id: str,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_READ)),
):
    """List remote branches, annotated with the environment each one backs.

    This is a DISCOVERY view, not the environment list: it reports what the
    remote repository actually contains, which is what makes an unregistered
    ``env/*`` branch (an orphan left by a failed create, or a branch pushed from
    outside the platform) visible so it can be adopted. Users pick environments
    from ``GET /{workspace_id}/environments``, which is served by the registry.
    """
    from datacode_common.naming import env_of

    branches = await get_workspace_manager().remote_branches(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
    )
    return {
        "workspace_id": workspace_id,
        "branches": [
            {
                "name": branch,
                "environment": env_of(branch),
                "managed": env_of(branch) is not None,
            }
            for branch in branches
        ],
    }


@router.post("/bind-git")
async def bind_git_repo(
    req: BindGitRepoRequest,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_GIT_BIND)),
):
    """Bind a remote Git repository to a workspace and push the current code."""
    url = await get_workspace_manager().bind_git_repo(
        workspace_id=req.workspace_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
        git_repo_url=req.git_repo_url,
    )
    return {"success": True, "workspace_id": req.workspace_id, "git_repo_url": url}


@router.post("/sync")
async def sync_workspace(
    req: SyncWorkspaceRequest,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_GIT_PUSH)),
):
    """Commit and push one environment working copy to the remote repository."""
    await get_workspace_manager().sync_to_remote(
        workspace_id=req.workspace_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
        message=req.message,
        environment=req.environment,
    )
    return {"success": True}


@router.delete("/{workspace_id}/members/{member_user_id}")
async def cleanup_member_working_copy(
    workspace_id: str,
    member_user_id: str,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_MEMBER_WRITE)),
):
    """Drop a revoked member's working copy.

    Access revocation itself is performed by the gateway, which owns the role
    bindings; this endpoint only removes the disposable checkout on disk.
    """
    removed = await get_workspace_manager().cleanup_member_copy(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        target_user_id=member_user_id,
    )
    return {"success": True, "workspace_id": workspace_id, "copy_removed": removed}


@router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_DELETE)),
):
    """Delete a workspace and everything the platform holds for it.

    There is no ``delete_remote`` parameter: the remote Git repository (and its
    branches) is always preserved. It may predate the workspace or be shared, and
    deleting a user's repository as a side effect is not the platform's call.

    The per-step counts are returned so the UI and an operator can see what was
    actually released rather than trusting a bare success flag.
    """
    result = await get_workspace_manager().delete(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
    )
    return {"success": True, "workspace_id": workspace_id, **result}
