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
    GitDeleteBranchRequest,
    GitFetchRequest,
    GitInitRequest,
    GitPublishRequest,
    GitPullRequest,
    GitPushRequest,
    GitResetRequest,
    GitStageRequest,
    GitUnstageRequest,
    UpdateGitCredentialRequest,
)
from src.core.context_pool import get_context_pool
from src.utils.errors import ConflictError, GitRemoteAccessError, InvalidRequestError
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/git", tags=["git"])

# Providers for which account linking (OAuth) is reserved. Every other provider
# is personal-access-token only.
OAUTH_CAPABLE_PROVIDERS = ("github",)


async def _resolve_remote_auth(user_id: str, workspace_path: str, remote: str):
    """Resolve the credential that matches the workspace remote's host.

    Returns ``(ResolvedGitAuth, remote_url)``; ``remote_url`` is empty when the
    workspace has no such remote configured.
    """
    from src.workspace.git_credentials import resolve_auth_for_workspace

    return await resolve_auth_for_workspace(user_id, workspace_path, remote)


# --- Git Panel Operations ---

@router.get("/status")
async def git_status(workspace_path: WorkspacePath):
    """Get Git status including branch, changes, and ahead/behind counts."""
    from src.workspace.git_ops import GitOperations, run_git_async, strip_url_credentials

    changes = await asyncio.to_thread(GitOperations.list_changes, workspace_path)
    branch = await asyncio.to_thread(GitOperations.current_branch, workspace_path)

    # Enrich with remote info. The URL is stripped of any embedded credential
    # before it leaves the service: it is rendered in the UI.
    remote_result = await run_git_async(workspace_path, ["remote", "get-url", "origin"])
    remote_url = (
        strip_url_credentials(remote_result.stdout.strip())
        if remote_result.returncode == 0
        else ""
    )

    ahead, behind = 0, 0
    if remote_url:
        rev_result = await run_git_async(workspace_path, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if rev_result.returncode == 0:
            parts = rev_result.stdout.strip().split("\t")
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    return {
        "branch": branch or "main",
        "remote_url": remote_url,
        "ahead": ahead,
        "behind": behind,
        "changes": [
            {
                "path": change.path,
                "status": change.status,
                "staged": change.staged,
                "original_path": change.original_path,
            }
            for change in changes
        ],
        "is_clean": len(changes) == 0,
    }


@router.get("/init-status")
async def git_init_status(workspace_path: WorkspacePath):
    """Check if workspace has git initialized."""
    git_dir = os.path.join(workspace_path, ".git")
    return {"initialized": os.path.exists(git_dir)}


@router.post("/init")
async def git_init(req: GitInitRequest):
    """Initialize git in workspace.

    The default ignore rules are written first: ``GitOperations.init`` stages
    everything for the initial commit, so without them the runtime output
    (caches, logs, error archive) would be tracked from the very first commit.
    """
    from src.workspace.git_ops import GitOperations
    from src.workspace.layout import ensure_gitignore

    await asyncio.to_thread(ensure_gitignore, req.workspace_path)
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
    """Push commits to remote using the credential that matches the remote host."""
    from src.workspace.git_ops import GitRemoteReason, extract_git_host, push_async, run_git_async

    branch = req.branch
    if not branch:
        br_result = await run_git_async(req.workspace_path, ["branch", "--show-current"])
        branch = br_result.stdout.strip() if br_result.returncode == 0 else "main"

    resolved, remote_url = await _resolve_remote_auth(
        req.user_id, req.workspace_path, req.remote
    )
    if not remote_url:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace has no '{req.remote}' remote. Bind a Git repository first.",
        )
    if resolved.auth is None:
        raise GitRemoteAccessError(
            "push", GitRemoteReason.CREDENTIAL_MISSING, extract_git_host(remote_url)
        )

    result = await push_async(req.workspace_path, branch, req.remote, resolved.auth)
    if not result.ok:
        raise GitRemoteAccessError("push", result.reason, resolved.host, result.detail)
    return {"success": True, "message": f"Pushed to {req.remote}/{branch}"}


@router.post("/pull")
async def git_pull(req: GitPullRequest):
    """Pull from remote. INVALIDATES context (working tree changes)."""
    from src.workspace.git_ops import pull_async, run_git_async

    branch = req.branch
    if not branch:
        br_result = await run_git_async(req.workspace_path, ["branch", "--show-current"])
        branch = br_result.stdout.strip() if br_result.returncode == 0 else "main"

    resolved, remote_url = await _resolve_remote_auth(
        req.user_id, req.workspace_path, req.remote
    )
    if not remote_url:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace has no '{req.remote}' remote. Bind a Git repository first.",
        )

    # A public repository can be pulled without a credential, so a missing
    # credential is not rejected up-front here (unlike push).
    result = await pull_async(req.workspace_path, branch, req.remote, resolved.auth)
    if not result.ok:
        raise GitRemoteAccessError("pull", result.reason, resolved.host, result.detail)

    # CRITICAL: Invalidate context after pull (working tree changed)
    await get_context_pool().invalidate_all_for_workspace(req.workspace_path)

    return {"success": True, "message": f"Pulled from {req.remote}/{branch}"}


@router.post("/fetch")
async def git_fetch(req: GitFetchRequest):
    """Update remote-tracking refs. Read-only for the working tree."""
    from src.workspace.git_ops import fetch_async

    resolved, remote_url = await _resolve_remote_auth(
        req.user_id, req.workspace_path, req.remote
    )
    if not remote_url:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace has no '{req.remote}' remote. Bind a Git repository first.",
        )

    # A public repository can be fetched anonymously, so a missing credential is
    # not rejected up-front.
    result = await fetch_async(req.workspace_path, req.remote, req.prune, resolved.auth)
    if not result.ok:
        raise GitRemoteAccessError("fetch", result.reason, resolved.host, result.detail)
    return {"success": True, "message": f"Fetched from {req.remote}"}


@router.post("/checkout")
async def git_checkout(req: GitCheckoutRequest):
    """Check out a branch, optionally creating it from a start point.

    Passing ``start_point`` + ``track`` (e.g. ``origin/dev``) materializes a
    remote branch locally with upstream tracking configured, so later pull/push
    need no arguments.

    INVALIDATES context (working tree changes).
    """
    from src.workspace.git_ops import (
        GitOperations,
        is_valid_branch_name,
        is_valid_start_point,
    )

    if not is_valid_branch_name(req.branch):
        raise InvalidRequestError(f"Invalid branch name: {req.branch}")
    if req.start_point and not is_valid_start_point(req.start_point):
        raise InvalidRequestError(f"Invalid start point: {req.start_point}")

    result = await asyncio.to_thread(
        GitOperations.checkout,
        req.workspace_path,
        req.branch,
        req.create,
        req.start_point,
        req.track,
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.detail or "Git checkout failed")

    # CRITICAL: Invalidate context after branch switch
    await get_context_pool().invalidate_all_for_workspace(req.workspace_path)

    return {"success": True, "branch": req.branch}


@router.post("/reset")
async def git_reset(req: GitResetRequest):
    """Move the current branch to an earlier commit.

    ``hard`` also discards working-tree changes, so the frontend confirms that
    mode explicitly before calling. INVALIDATES context for every mode that can
    touch the index or the working tree.
    """
    from src.workspace.git_ops import RESET_MODES, GitOperations, is_valid_commit_hash

    if not is_valid_commit_hash(req.commit_hash):
        raise InvalidRequestError(f"Invalid commit hash: {req.commit_hash}")
    if req.mode not in RESET_MODES:
        raise InvalidRequestError(
            f"Invalid reset mode: {req.mode}. Expected one of {', '.join(RESET_MODES)}."
        )

    result = await asyncio.to_thread(
        GitOperations.reset, req.workspace_path, req.commit_hash, req.mode
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.detail or "Git reset failed")

    await get_context_pool().invalidate_all_for_workspace(req.workspace_path)

    return {
        "success": True,
        "commit_hash": req.commit_hash,
        "mode": req.mode,
    }


@router.post("/branch/delete")
async def git_delete_branch(req: GitDeleteBranchRequest):
    """Delete a local branch. The checked-out branch cannot be deleted."""
    from src.workspace.git_ops import GitOperations, is_valid_branch_name

    if not is_valid_branch_name(req.branch):
        raise InvalidRequestError(f"Invalid branch name: {req.branch}")

    current = await asyncio.to_thread(GitOperations.current_branch, req.workspace_path)
    if current == req.branch:
        raise ConflictError(
            f"Branch '{req.branch}' is checked out. Switch to another branch first."
        )

    result = await asyncio.to_thread(
        GitOperations.delete_branch, req.workspace_path, req.branch, req.force
    )
    if not result.ok:
        raise HTTPException(
            status_code=409, detail=result.detail or "Git branch deletion failed"
        )
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
    from src.workspace.git_ops import (
        GitAuth,
        GitOperations,
        extract_git_host,
        push_async,
    )
    from src.workspace.repository import UserGitCredentialRepository

    cred_repo = UserGitCredentialRepository()
    try:
        # The credential must belong to the provider we are publishing to;
        # the user's default credential may point at a different provider.
        cred = await cred_repo.get_for_provider(req.user_id, req.provider)
    except Exception:
        cred = None

    if not cred:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No Git credential found for provider '{req.provider}'. "
                f"Add one in Settings first."
            ),
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

    auth = GitAuth(
        token=cred.token, provider=cred.provider or "", username=cred.username or ""
    )
    await asyncio.to_thread(
        GitOperations.add_remote, req.workspace_path, repo_info.clone_url
    )
    push_result = await push_async(req.workspace_path, "main", "origin", auth)
    if not push_result.ok:
        raise GitRemoteAccessError(
            "publish",
            push_result.reason,
            extract_git_host(repo_info.clone_url),
            push_result.detail,
        )

    return {
        "success": True,
        "remote_url": repo_info.clone_url,
        "html_url": repo_info.html_url,
        "message": f"Published to {repo_info.html_url}",
    }


# --- Git Credentials ---

@router.get("/credentials")
async def list_git_credentials(user_id: str):
    """List all Git credentials for a user."""
    from src.workspace.git_ops import credential_host
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
                    # Host the credential authenticates against. Remote
                    # operations match on this, so the UI surfaces it too.
                    "host": credential_host(c.provider, c.server_url or ""),
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
    """Create a Git credential.

    Only personal access tokens can be created here. Account linking (OAuth) is
    reserved for GitHub and, until that flow is enabled, is rejected instead of
    silently persisting a token-less row that would break every Git operation.

    At most ONE credential per host is allowed: remote operations resolve the
    credential by the remote's host, so two credentials for the same host would
    make the choice ambiguous.
    """
    from src.workspace.git_ops import credential_host
    from src.workspace.repository import UserGitCredentialRepository

    provider = (req.provider or "").strip().lower()
    auth_type = (req.auth_type or "pat").strip().lower()
    if not provider:
        raise InvalidRequestError("Provider is required")

    if auth_type == "oauth":
        if provider not in OAUTH_CAPABLE_PROVIDERS:
            raise InvalidRequestError(
                f"Provider '{provider}' supports personal access tokens only"
            )
        raise HTTPException(
            status_code=501,
            detail=(
                "Linking a GitHub account is not available yet. "
                "Use a personal access token for now."
            ),
        )
    if auth_type != "pat":
        raise InvalidRequestError(f"Unsupported auth_type: {auth_type}")

    if not (req.token or "").strip():
        raise InvalidRequestError("Token is required")
    if not (req.server_url or "").strip():
        raise InvalidRequestError("Server URL is required")

    host = credential_host(provider, req.server_url)
    if not host:
        raise InvalidRequestError("Server URL must contain a valid host")

    repo = UserGitCredentialRepository()
    try:
        existing = await repo.get_for_host(req.user_id, host)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A credential for {host} already exists "
                    f"('{existing.nickname}'). Edit that one instead."
                ),
            )

        credential_id = str(uuid.uuid4())
        nickname = req.nickname or f"{provider.capitalize()} ({host})"

        cred = await repo.create(
            credential_id=credential_id,
            user_id=req.user_id,
            provider=provider,
            nickname=nickname,
            auth_type=auth_type,
            token=req.token.strip(),
            email=req.email,
            username=req.username,
            server_url=req.server_url.strip(),
        )
        logger.info(
            "git_credential_created", credential_id=cred.id, provider=provider, host=host
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
    from src.workspace.git_ops import credential_host
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    current = await repo.get_by_id(credential_id)
    if not current:
        raise HTTPException(status_code=404, detail="Credential not found")

    updates = {}
    if req.nickname:
        updates["nickname"] = req.nickname
    if req.token:
        updates["token"] = req.token.strip()
    if req.email:
        updates["email"] = req.email
    if req.username:
        updates["username"] = req.username
    if req.server_url:
        # Moving a credential to another host must not create a second
        # credential for that host, which would make resolution ambiguous.
        host = credential_host(current.provider, req.server_url)
        if not host:
            raise InvalidRequestError("Server URL must contain a valid host")
        clash = await repo.get_for_host(current.user_id, host)
        if clash is not None and clash.id != credential_id:
            raise HTTPException(
                status_code=409,
                detail=f"A credential for {host} already exists ('{clash.nickname}').",
            )
        updates["server_url"] = req.server_url.strip()

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
