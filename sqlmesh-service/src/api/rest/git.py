"""Git operation endpoints.

Handles Git panel operations (status, stage, commit, push, pull, branches, checkout, publish).
CRITICAL: Operations that change the working tree invalidate the Context Pool.
"""

import asyncio
import os
import uuid

from fastapi import APIRouter, HTTPException

from src.api.deps import WorkspacePath

from src.api.models.requests import (
    CreateGitCredentialRequest,
    GitCheckoutRequest,
    GitCommitRequest,
    GitInitRequest,
    GitPublishRequest,
    GitPullRequest,
    GitPushRequest,
    GitStageRequest,
    GitUnstageRequest,
    UpdateGitCredentialRequest,
)
from src.core.context_pool import get_context_pool
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/git", tags=["git"])


# --- Git Panel Operations ---

@router.get("/status")
async def git_status(workspace_path: WorkspacePath):
    """Get Git status including branch, changes, and ahead/behind counts."""
    from src.workspace.git_ops import GitOperations, run_git_async

    status = await asyncio.to_thread(GitOperations.status, workspace_path)

    # Enrich with remote info
    remote_result = await run_git_async(workspace_path, ["remote", "get-url", "origin"])
    remote_url = remote_result.stdout.strip() if remote_result.returncode == 0 else ""

    ahead, behind = 0, 0
    if remote_url:
        rev_result = await run_git_async(workspace_path, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if rev_result.returncode == 0:
            parts = rev_result.stdout.strip().split("\t")
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    # Build structured file changes with staged info
    changes = []
    porcelain = await run_git_async(workspace_path, ["status", "--porcelain"])
    if porcelain.returncode == 0:
        for line in porcelain.stdout.strip().split("\n"):
            if not line.strip():
                continue
            index_status = line[0]
            worktree_status = line[1]
            file_path = line[3:]

            if index_status == "?" and worktree_status == "?":
                changes.append({"path": file_path, "status": "A", "staged": False})
            elif index_status != " " and index_status != "?":
                changes.append({"path": file_path, "status": index_status, "staged": True})
            elif worktree_status != " ":
                changes.append({"path": file_path, "status": worktree_status, "staged": False})

    return {
        "branch": status.get("branch", "main"),
        "remote_url": remote_url,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "is_clean": len(changes) == 0,
    }


@router.get("/init-status")
async def git_init_status(workspace_path: WorkspacePath):
    """Check if workspace has git initialized."""
    git_dir = os.path.join(workspace_path, ".git")
    return {"initialized": os.path.exists(git_dir)}


@router.post("/init")
async def git_init(req: GitInitRequest):
    """Initialize git in workspace."""
    from src.workspace.git_ops import GitOperations
    success = await asyncio.to_thread(GitOperations.init, req.workspace_path)
    return {"success": success}


@router.post("/stage")
async def git_stage(req: GitStageRequest):
    """Stage files for commit."""
    from src.workspace.git_ops import run_git_async

    if not req.paths:
        result = await run_git_async(req.workspace_path, ["add", "-A"])
    else:
        result = await run_git_async(req.workspace_path, ["add"] + req.paths)

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git stage failed: {result.stderr}")
    return {"success": True, "staged_count": len(req.paths) if req.paths else -1}


@router.post("/unstage")
async def git_unstage(req: GitUnstageRequest):
    """Unstage files from staging area."""
    from src.workspace.git_ops import run_git_async

    if not req.paths:
        result = await run_git_async(req.workspace_path, ["reset", "HEAD"])
    else:
        result = await run_git_async(req.workspace_path, ["reset", "HEAD", "--"] + req.paths)

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git unstage failed: {result.stderr}")
    return {"success": True}


@router.post("/commit")
async def git_commit(req: GitCommitRequest):
    """Commit staged changes."""
    from src.workspace.git_ops import run_git_async

    result = await run_git_async(req.workspace_path, ["commit", "-m", req.message])
    if result.returncode != 0:
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return {"success": False, "commit_hash": None, "message": "Nothing to commit"}
        raise HTTPException(status_code=500, detail=f"Git commit failed: {result.stderr}")

    hash_result = await run_git_async(req.workspace_path, ["rev-parse", "HEAD"])
    commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
    return {"success": True, "commit_hash": commit_hash}


@router.post("/push")
async def git_push(req: GitPushRequest):
    """Push commits to remote."""
    from src.workspace.git_ops import GitOperations, run_git_async

    branch = req.branch
    if not branch:
        br_result = await run_git_async(req.workspace_path, ["branch", "--show-current"])
        branch = br_result.stdout.strip() if br_result.returncode == 0 else "main"

    success = await asyncio.to_thread(
        GitOperations.push, req.workspace_path, branch=branch, remote=req.remote
    )
    if not success:
        raise HTTPException(status_code=500, detail="Git push failed")
    return {"success": True, "message": f"Pushed to {req.remote}/{branch}"}


@router.post("/pull")
async def git_pull(req: GitPullRequest):
    """Pull from remote. INVALIDATES context (working tree changes)."""
    from src.workspace.git_ops import GitOperations, run_git_async

    branch = req.branch
    if not branch:
        br_result = await run_git_async(req.workspace_path, ["branch", "--show-current"])
        branch = br_result.stdout.strip() if br_result.returncode == 0 else "main"

    success = await asyncio.to_thread(
        GitOperations.pull, req.workspace_path, branch=branch, remote=req.remote
    )
    if not success:
        raise HTTPException(status_code=500, detail="Git pull failed")

    # CRITICAL: Invalidate context after pull (working tree changed)
    await get_context_pool().invalidate_all_for_workspace(req.workspace_path)

    return {"success": True, "message": f"Pulled from {req.remote}/{branch}"}


@router.post("/checkout")
async def git_checkout(req: GitCheckoutRequest):
    """Checkout or create a branch. INVALIDATES context (working tree changes)."""
    from src.workspace.git_ops import run_git_async

    if req.create:
        result = await run_git_async(req.workspace_path, ["checkout", "-b", req.branch])
    else:
        result = await run_git_async(req.workspace_path, ["checkout", req.branch])

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git checkout failed: {result.stderr}")

    # CRITICAL: Invalidate context after branch switch
    await get_context_pool().invalidate_all_for_workspace(req.workspace_path)

    return {"success": True, "branch": req.branch}


@router.get("/branches")
async def git_branches(workspace_path: WorkspacePath):
    """List all branches (local + remote)."""
    from src.workspace.git_ops import run_git_async

    current_result = await run_git_async(workspace_path, ["branch", "--show-current"])
    current_branch = current_result.stdout.strip() if current_result.returncode == 0 else "main"

    result = await run_git_async(workspace_path, ["branch", "-a", "--format=%(refname:short)\t%(objectname:short)\t%(subject)"])
    branches = []
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            name = parts[0] if len(parts) > 0 else ""
            commit_hash = parts[1] if len(parts) > 1 else ""
            message = parts[2] if len(parts) > 2 else ""
            is_remote = name.startswith("origin/")
            branches.append({
                "name": name,
                "is_remote": is_remote,
                "last_commit_hash": commit_hash,
                "last_commit_message": message,
            })

    return {"branches": branches, "current_branch": current_branch}


@router.get("/log")
async def git_log(workspace_path: WorkspacePath, branch: str = "", limit: int = 50):
    """Return commit history (newest first). Timestamps are UTC ISO 8601."""
    from src.workspace.git_ops import run_git_async

    pretty = "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"
    args = ["log", pretty, f"--max-count={int(limit)}"]
    if branch:
        args.append(branch)

    result = await run_git_async(workspace_path, args)
    commits = []
    if result.returncode == 0:
        for record in result.stdout.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            fields = record.split("\x1f")
            if len(fields) < 6:
                continue
            commits.append({
                "hash": fields[0],
                "short_hash": fields[1],
                "author_name": fields[2],
                "author_email": fields[3],
                "date": fields[4],
                "message": fields[5],
            })

    return {"commits": commits, "branch": branch}


@router.get("/commit-files")
async def git_commit_files(workspace_path: WorkspacePath, commit_hash: str):
    """Return the list of files changed in a single commit."""
    from src.workspace.git_ops import run_git_async

    result = await run_git_async(workspace_path, ["show", "--name-status", "--format=", commit_hash])
    files = []
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            status_code = parts[0][:1] if parts else "M"
            file_path = parts[-1] if len(parts) > 1 else ""
            if not file_path:
                continue
            files.append({"path": file_path, "status": status_code})

    return {"commit_hash": commit_hash, "files": files}


@router.get("/commit-file-diff")
async def git_commit_file_diff(workspace_path: WorkspacePath, commit_hash: str, file_path: str):
    """Return the patch diff for one file in a commit."""
    from src.workspace.git_ops import run_git_async

    result = await run_git_async(
        workspace_path,
        ["show", "--format=", "--patch", "--find-renames", commit_hash, "--", file_path],
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git diff failed: {result.stderr}")

    return {
        "commit_hash": commit_hash,
        "file_path": file_path,
        "diff": result.stdout or "No textual diff available for this file.",
    }


@router.post("/publish")
async def git_publish(req: GitPublishRequest):
    """Initialize repo + create remote + push (full publish flow)."""
    from src.workspace.git_ops import GitOperations
    from src.workspace.repository import UserGitCredentialRepository

    cred_repo = UserGitCredentialRepository()
    try:
        cred = await cred_repo.get_default(req.user_id)
    except Exception:
        cred = None

    if not cred:
        raise HTTPException(
            status_code=400,
            detail="No Git credential found. Please configure Git access first.",
        )

    # Ensure local git is initialized
    await asyncio.to_thread(GitOperations.init, req.workspace_path)

    # Create remote repository
    repo_info = await GitOperations.create_remote_repo(
        name=req.repo_name,
        token=cred.token,
        server_url=cred.server_url or "",
        provider=req.provider,
        private=req.is_private,
    )
    if not repo_info:
        raise HTTPException(status_code=500, detail="Failed to create remote repository")

    # Add remote and push
    await asyncio.to_thread(
        GitOperations.add_remote, req.workspace_path, repo_info.clone_url, cred.token
    )
    push_success = await asyncio.to_thread(
        GitOperations.push, req.workspace_path, branch="main", remote="origin"
    )

    return {
        "success": push_success,
        "remote_url": repo_info.clone_url,
        "html_url": repo_info.html_url,
        "message": f"Published to {repo_info.html_url}" if push_success else "Push failed",
    }


# --- Git Credentials ---

@router.get("/credentials")
async def list_git_credentials(user_id: str):
    """List all Git credentials for a user."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    try:
        creds = await repo.list_by_user(user_id)
        return {
            "credentials": [
                {
                    "id": c.id,
                    "provider": c.provider,
                    "nickname": c.nickname,
                    "auth_type": c.auth_type,
                    "email": c.email or "",
                    "username": c.username or "",
                    "server_url": c.server_url or "",
                    "is_default": c.is_default == 1,
                    "oauth_account": c.oauth_account or "",
                    "created_at": c.created_at.isoformat() if c.created_at else "",
                }
                for c in creds
            ]
        }
    except Exception as e:
        logger.error("list_git_credentials_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list credentials: {e}")


@router.post("/credentials")
async def create_git_credential(req: CreateGitCredentialRequest):
    """Create a new Git credential (PAT or OAuth)."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    try:
        if req.auth_type == "oauth":
            has_existing = await repo.has_oauth(req.user_id, req.provider)
            if has_existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"OAuth credential already exists for {req.provider}",
                )

        credential_id = str(uuid.uuid4())
        nickname = req.nickname or f"{req.provider.capitalize()} ({req.auth_type.upper()})"

        cred = await repo.create(
            credential_id=credential_id,
            user_id=req.user_id,
            provider=req.provider,
            nickname=nickname,
            auth_type=req.auth_type,
            token=req.token,
            email=req.email,
            username=req.username,
            server_url=req.server_url,
        )
        return {
            "success": True,
            "credential": {
                "id": cred.id,
                "provider": cred.provider,
                "nickname": cred.nickname,
                "auth_type": cred.auth_type,
                "is_default": cred.is_default == 1,
                "created_at": cred.created_at.isoformat() if cred.created_at else "",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_git_credential_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create credential: {e}")


@router.put("/credentials/{credential_id}")
async def update_git_credential(credential_id: str, req: UpdateGitCredentialRequest):
    """Update an existing Git credential."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    updates = {}
    if req.nickname:
        updates["nickname"] = req.nickname
    if req.token:
        updates["token"] = req.token
    if req.email:
        updates["email"] = req.email
    if req.username:
        updates["username"] = req.username
    if req.server_url:
        updates["server_url"] = req.server_url

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    success = await repo.update(credential_id, **updates)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


@router.patch("/credentials/{credential_id}/default")
async def set_default_git_credential(credential_id: str, user_id: str):
    """Set a credential as the default for a user."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    success = await repo.set_default(user_id, credential_id)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


@router.delete("/credentials/{credential_id}")
async def delete_git_credential(credential_id: str, user_id: str):
    """Delete a Git credential."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    success = await repo.delete(credential_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}
