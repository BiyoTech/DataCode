"""Workspace management endpoints."""

from fastapi import APIRouter, HTTPException

from src.api.models.requests import (
    BindGitRepoRequest,
    CreateWorkspaceRequest,
    SyncWorkspaceRequest,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspace"])


@router.post("")
async def create_workspace(req: CreateWorkspaceRequest):
    """Create a new workspace (SQLMesh project directory)."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    ws = await mgr.create(
        user_id=req.user_id,
        name=req.name,
        dialect=req.dialect,
        git_repo_url=req.git_repo_url,
    )
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "path": ws.local_path,
        "git_repo_url": ws.git_repo_url,
        "created_at": ws.created_at.isoformat(),
    }


@router.get("")
async def list_workspaces(user_id: str):
    """List workspaces for a user."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    workspaces = await mgr.list_workspaces(user_id)
    return {
        "workspaces": [
            {
                "workspace_id": ws.workspace_id,
                "name": ws.name,
                "path": ws.local_path,
                "status": ws.status,
                "git_repo_url": ws.git_repo_url,
                "created_at": ws.created_at.isoformat(),
            }
            for ws in workspaces
        ]
    }


@router.post("/bind-git")
async def bind_git_repo(req: BindGitRepoRequest):
    """Bind a remote Git repository to an existing workspace."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    success = await mgr.bind_git_repo(
        workspace_id=req.workspace_id,
        git_repo_url=req.git_repo_url,
        user_id=req.user_id,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"success": True, "workspace_id": req.workspace_id, "git_repo_url": req.git_repo_url}


@router.post("/sync")
async def sync_workspace(req: SyncWorkspaceRequest):
    """Commit and push workspace changes to remote Git."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    success = await mgr.sync_to_remote(
        workspace_id=req.workspace_id,
        message=req.message,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Sync failed (no remote bound or push error)")
    return {"success": True}


@router.post("/{workspace_id}/ensure-local")
async def ensure_workspace_local(workspace_id: str):
    """Ensure workspace exists locally (clone from Git if needed)."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    local_path = await mgr.ensure_local(workspace_id)
    if not local_path:
        raise HTTPException(status_code=404, detail="Workspace not recoverable")
    return {"workspace_id": workspace_id, "local_path": local_path}


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str, delete_remote: bool = False):
    """Delete a workspace (soft-delete)."""
    from src.workspace.manager import get_workspace_manager

    mgr = get_workspace_manager()
    success = await mgr.delete(workspace_id, delete_remote=delete_remote)
    if not success:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"success": True}
