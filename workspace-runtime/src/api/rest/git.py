"""Git operation endpoints.

Handles the day-to-day operations of ONE environment working copy: status,
stage, commit, push, pull, fetch, reset and log.

Credentials are NOT managed here. They are user account material owned by the
control plane (``control-service/src/api/rest/git_credentials.py``); this service
only READS them, through internal repository calls, to authenticate a remote
operation. The CRUD endpoints that used to live here were dead traffic — the
gateway routes every ``/git/credentials*`` request to the control plane — and
they made a control-plane table have a second writer, which the Global/Cell
database split cannot allow.

Branch lifecycle is deliberately absent. A branch is an Environment's code
carrier (1:1), so creating, switching and deleting one is orchestrated by the
environment registry (``/api/v1/workspaces/{id}/environments``), which also
manages the matching worktree, SQLMesh virtual environment and deployment.
Switching environment means opening another worktree, never a ``git checkout``,
so uncommitted work can never block it. ``GET /branches`` stays as a read-only
view annotated with environment ownership.

CRITICAL: Operations that change the working tree invalidate cached metadata.
"""

import asyncio
import os
import typing as t

from datacode_common.rbac import PERM_WORKSPACE_ENV_PROMOTE
from fastapi import APIRouter, HTTPException

from src.api.deps import Caller, WorkspacePath
from src.api.models.requests import (
    GitCommitRequest,
    GitFetchRequest,
    GitInitRequest,
    GitMergeRequest,
    GitPublishRequest,
    GitPullRequest,
    GitPushRequest,
    GitResetRequest,
    GitStageRequest,
    GitSyncRequest,
    GitUnstageRequest,
)
from src.executor.process import get_executor_manager
from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.errors import ConflictError, GitRemoteAccessError, InvalidRequestError
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/git", tags=["git"])


async def _resolve_remote_auth(user_id: str, workspace_path: str, remote: str):
    """Resolve the credential that matches the workspace remote's host.

    Returns ``(ResolvedGitAuth, remote_url)``; ``remote_url`` is empty when the
    workspace has no such remote configured.
    """
    from src.workspace.git_credentials import resolve_auth_for_workspace

    return await resolve_auth_for_workspace(user_id, workspace_path, remote)


def _remote_failure(
    operation: str,
    reason: str,
    host: str,
    workspace_path: str,
    detail: str = "",
) -> GitRemoteAccessError:
    """Log and archive a rejected remote Git operation, then return the error.

    Every remote failure funnels through here so the user is never told to
    "check the workspace logs" about something that was never written there.
    The archive gives the response an ``error_log_id`` pointing at the full
    record, which matters most for ``unknown`` — the reason code that carries
    no actionable sentence of its own.

    Returns the error rather than raising it so call sites keep an explicit
    ``raise`` and static analysis still sees the control flow.
    """
    from src.utils.error_archive import ErrorArchive, ErrorArchiveContext

    error = GitRemoteAccessError(operation, reason, host, detail)
    logger.error(
        "git_remote_failed",
        operation=f"git.{operation}",
        git_error_reason=reason,
        host=host,
        error_code=error.code.value,
        stderr=detail,
    )
    error.error_log_id = ErrorArchive.record(
        exc=error,
        service_error=error,
        archive_context=ErrorArchiveContext(
            workspace_path=workspace_path,
            operation=f"git.{operation}",
            context={"git_error_reason": reason, "host": host, "stderr": detail},
        ),
    )
    return error


# --- Git Panel Operations ---

@router.get("/status")
async def git_status(workspace_path: WorkspacePath):
    """Get Git status including branch, changes, and ahead/behind counts."""
    from src.git.ops import GitOperations, run_git_async, strip_url_credentials

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
        rev_args = ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]
        rev_result = await run_git_async(workspace_path, rev_args)
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

    ``GitOperations.init`` writes the default ignore rules before its first
    ``git add`` (a rule only stops a file from being ADDED, so writing them
    afterwards would leave the runtime output tracked for good). This endpoint
    keeps its own call as well, so the rules land even on a workspace whose
    ``.git`` already exists and therefore skips the init path entirely.
    """
    from src.files.layout import ensure_gitignore
    from src.git.ops import GitOperations

    await asyncio.to_thread(ensure_gitignore, req.workspace_path)
    success = await asyncio.to_thread(GitOperations.init, req.workspace_path)
    return {"success": success}


@router.post("/stage")
async def git_stage(req: GitStageRequest):
    """Stage files for commit."""
    from src.git.ops import run_git_async

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
    from src.git.ops import run_git_async

    if not req.paths:
        result = await run_git_async(req.workspace_path, ["reset", "HEAD"])
    else:
        result = await run_git_async(req.workspace_path, ["reset", "HEAD", "--"] + req.paths)

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git unstage failed: {result.stderr}")
    return {"success": True}


@router.post("/commit")
async def git_commit(req: GitCommitRequest):
    """Commit staged changes, authored by the acting user.

    The author identity is resolved from the user's git credential (matching
    the remote's host, else the default credential, else the platform
    identity) and injected for THIS command only. A bare ``git commit`` relied
    on the process user's global gitconfig — present on a dev machine, absent
    in the container image, where the commit failed with "unable to
    auto-detect email address".
    """
    from src.git.ops import author_args, run_git_async
    from src.sqlmesh.env_locks import check_environment_lock
    from src.workspace.git_credentials import resolve_commit_identity

    # A commit landing mid-apply would desynchronize the recorded applied
    # commit from the code that actually ran; reject while apply holds the
    # environment lock (advisory pre-check, non-blocking).
    await check_environment_lock(req.workspace_path, operation="commit")

    remote = await run_git_async(req.workspace_path, ["remote", "get-url", "origin"])
    remote_url = remote.stdout.strip() if remote.returncode == 0 else ""
    author = await resolve_commit_identity(req.user_id, remote_url)

    result = await run_git_async(
        req.workspace_path, author_args(author) + ["commit", "-m", req.message]
    )
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
    from src.git.ops import GitRemoteReason, extract_git_host, push_async, run_git_async

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
        raise _remote_failure(
            "push",
            GitRemoteReason.CREDENTIAL_MISSING,
            extract_git_host(remote_url),
            req.workspace_path,
        )

    result = await push_async(req.workspace_path, branch, req.remote, resolved.auth)
    if not result.ok:
        raise _remote_failure(
            "push", result.reason, resolved.host, req.workspace_path, result.detail
        )
    return {"success": True, "message": f"Pushed to {req.remote}/{branch}"}


@router.post("/pull")
async def git_pull(req: GitPullRequest):
    """Integrate the remote branch into this working copy.

    Not a raw ``git pull``: the integration strategy is decided by the platform
    (fast-forward when possible, otherwise a real merge commit) so the result
    never depends on ambient ``pull.rebase`` configuration and a DIVERGENT
    environment branch can be pulled at all.

    A conflicted outcome is a 200: the conflicted files are now in the working
    tree, which is the only place a human can resolve them — the same contract
    as ``POST /sync``. Uncommitted tracked changes are a 409 instead, because
    nothing was written and the user has to commit or discard first.

    INVALIDATES the context whenever the working tree changed.
    """
    from src.git.ops import (
        PULL_BLOCKED,
        PULL_UP_TO_DATE,
        pull_async,
        run_git_async,
    )
    from src.sqlmesh.env_locks import check_environment_lock

    branch = req.branch
    if not branch:
        br_result = await run_git_async(req.workspace_path, ["branch", "--show-current"])
        branch = br_result.stdout.strip() if br_result.returncode == 0 else "main"

    # An integration landing mid-apply would desynchronize the recorded applied
    # commit from the code that actually ran (same reason as commit / sync).
    await check_environment_lock(req.workspace_path, operation="pull")

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
        if result.outcome == PULL_BLOCKED:
            raise ConflictError(
                f"This working copy has uncommitted changes ({result.detail}). "
                f"Commit or discard them before pulling '{branch}'."
            )
        raise _remote_failure(
            "pull", result.reason, resolved.host, req.workspace_path, result.detail
        )

    # CRITICAL: the context caches the loaded project, so it must be dropped as
    # soon as the working tree changed — including a conflicted merge, whose
    # markers are real file content.
    if result.outcome != PULL_UP_TO_DATE:
        # A branch switch retires the tree the sandbox's Context describes;
        # stop its children rather than refresh them, and drop the runtime's
        # cached metadata (same trade at this call site).
        get_executor_manager().invalidate(req.workspace_path)
        get_metadata_context().invalidate(req.workspace_path)

    return {
        "success": True,
        "branch": branch,
        "outcome": result.outcome,
        "conflicted": bool(result.conflicts),
        "conflicts": result.conflicts,
        "commit": result.commit,
        "integrated_commits": result.integrated_commits,
        "message": f"Pulled from {req.remote}/{branch}",
    }


@router.post("/fetch")
async def git_fetch(req: GitFetchRequest):
    """Update remote-tracking refs. Read-only for the working tree."""
    from src.git.ops import fetch_async

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
        raise _remote_failure(
            "fetch", result.reason, resolved.host, req.workspace_path, result.detail
        )
    return {"success": True, "message": f"Fetched from {req.remote}"}


@router.post("/reset")
async def git_reset(req: GitResetRequest):
    """Move the current branch to an earlier commit.

    ``hard`` also discards working-tree changes, so the frontend confirms that
    mode explicitly before calling. INVALIDATES context for every mode that can
    touch the index or the working tree.
    """
    from src.git.ops import RESET_MODES, GitOperations, is_valid_commit_hash

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

    # A branch switch retires the tree the sandbox's Context describes;
    # stop its children rather than refresh them, and drop the runtime's
    # cached metadata (same trade at this call site).
    get_executor_manager().invalidate(req.workspace_path)
    get_metadata_context().invalidate(req.workspace_path)

    return {
        "success": True,
        "commit_hash": req.commit_hash,
        "mode": req.mode,
    }


@router.get("/branches")
async def git_branches(workspace_path: WorkspacePath):
    """List all branches (local + remote), annotated with environment ownership.

    Read-only by design: branches are not created, switched or deleted here.
    A branch is an Environment's code carrier, so its whole lifecycle belongs to
    the environment registry (``/api/v1/workspaces/{id}/environments``). This
    endpoint exists so the Git panel can show WHICH environment a branch backs.

    Each entry carries:

    * ``environment`` - the environment claiming the branch, or ``null`` for a
      branch outside the platform's ``env/`` namespace (e.g. ``release/1.0``
      brought in from an external repository);
    * ``managed``     - whether the platform owns the branch's lifecycle.
      Un-managed branches are read-only: they cannot be planned or applied and
      must be adopted as an environment first.
    """
    from datacode_common.naming import env_of

    from src.git.ops import run_git_async

    current_result = await run_git_async(workspace_path, ["branch", "--show-current"])
    current_branch = current_result.stdout.strip() if current_result.returncode == 0 else "main"

    branch_format = "--format=%(refname:short)\t%(objectname:short)\t%(subject)"
    result = await run_git_async(workspace_path, ["branch", "-a", branch_format])
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
            # Remote refs are prefixed with the remote name, which is not part
            # of the branch identity the naming algebra works on.
            local_name = name.split("/", 1)[1] if is_remote else name
            environment = env_of(local_name)
            branches.append({
                "name": name,
                "is_remote": is_remote,
                "last_commit_hash": commit_hash,
                "last_commit_message": message,
                "environment": environment,
                "managed": environment is not None,
            })

    return {
        "branches": branches,
        "current_branch": current_branch,
        "current_environment": env_of(current_branch),
    }


# --- Cross-branch merge (code promotion, step 1 of 2) ---
#
# Promotion is TWO explicit user actions, not one atomic endpoint: merge the
# code here, then plan & apply and reload the code location on the target
# environment. A merge conflict is an intermediate state only a human with a
# working tree can leave, so an "atomic promote" would inevitably degrade into
# "try, report conflict, wait for a human, retry" — the same sequence, only
# hidden inside a black box and much harder to audit or retry step by step.


def _require_platform_branch(branch: str, field: str) -> str:
    """Return the environment a branch backs, refusing platform-external ones.

    A merge changes what an environment's code IS, so both sides must be
    environments the platform owns. A branch outside the ``env/`` namespace
    (e.g. ``release/1.0`` from an existing repository) stays read-only until it
    is adopted as an environment, exactly as it is everywhere else in the UI.
    """
    from datacode_common.naming import env_of

    from src.git.ops import is_valid_branch_name

    if not is_valid_branch_name(branch):
        raise InvalidRequestError(f"Invalid {field}: {branch!r}")

    environment = env_of(branch)
    if environment is None:
        raise ConflictError(
            f"Branch '{branch}' ({field}) is outside the platform namespace and "
            f"cannot take part in a merge. Adopt it as an environment first."
        )
    return environment


async def _record_merge_event(
    workspace_path: str,
    environment: str,
    actor_user_id: str,
    succeeded: bool,
    commit_hash: str = "",
    detail: t.Optional[dict] = None,
) -> None:
    """Append a ``merge`` entry to the target environment's audit trail.

    Best-effort by design: the merge already happened in Git, so failing the
    response because the audit write failed would only hide a completed action.
    """
    try:
        from datacode_common.naming import ACTION_MERGE, environment_id

        from src.environments.repository import EnvironmentRepository
        from src.workspace.identity import resolve_workspace_identity

        identity = resolve_workspace_identity(workspace_path)
        await EnvironmentRepository().record_event(
            environment_id=environment_id(identity.workspace_id, environment),
            action=ACTION_MERGE,
            actor_user_id=actor_user_id,
            succeeded=succeeded,
            commit_hash=commit_hash,
            detail=detail,
        )
    except Exception as exc:
        logger.warning(
            "git_merge_event_not_recorded",
            workspace=workspace_path,
            environment=environment,
            error=str(exc),
        )


@router.get("/branch-diff")
async def git_branch_diff(
    workspace_path: WorkspacePath,
    source: str,
    target: str,
    caller: Caller,
    file_path: str = "",
    limit: int = 100,
    remote: str = "origin",
):
    """Preview merging ``source`` into ``target``: commits, files and conflicts.

    Read-only. The conflict verdict comes from the real merge machinery
    (``git merge-tree``), so the confirmation dialog can state whether the merge
    is able to land instead of guessing from changed paths. Pass ``file_path``
    to get that single file's patch, which is what opens a branch diff in the
    editor.

    The credential owner is the verified caller, like in ``POST /merge`` this
    previews: reading it from a query parameter would let a direct caller fetch
    the remote with someone else's token.
    """
    from src.git.merge import preview_merge

    resolved, _ = await _resolve_remote_auth(caller.user_id, workspace_path, remote)
    return await preview_merge(
        workspace_path,
        source_branch=source,
        target_branch=target,
        remote=remote,
        auth=resolved.auth,
        file_path=file_path,
        limit=limit,
    )


@router.post("/merge")
async def git_merge(req: GitMergeRequest, caller: Caller):
    """Merge one environment branch into another and push the result.

    Touches no working copy: the merge is computed with plumbing and published
    by pushing the merge commit straight to the remote, so a member holding the
    target branch checked out is never desynchronized behind their back. They
    pick the change up with their own explicit pull + reload.
    """
    from datacode_common.naming import PRODUCTION_ENV

    from src.git.merge import promote_merge
    from src.git.ops import GitRemoteReason, extract_git_host
    from src.utils.errors import PermissionDeniedError, ServiceError

    source_env = _require_platform_branch(req.source_branch, "source branch")
    target_env = _require_platform_branch(req.target_branch, "target branch")

    # Defence in depth on top of the gateway's route permission: merging into
    # production IS the promotion, and it is gated separately from an ordinary
    # push between development branches.
    if target_env == PRODUCTION_ENV and not caller.can(PERM_WORKSPACE_ENV_PROMOTE):
        raise PermissionDeniedError(
            "Merging into the production branch is a promotion",
            permission=PERM_WORKSPACE_ENV_PROMOTE,
        )

    resolved, remote_url = await _resolve_remote_auth(
        req.user_id, req.workspace_path, req.remote
    )
    if not remote_url:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace has no '{req.remote}' remote. Bind a Git repository first.",
        )
    if resolved.auth is None:
        raise _remote_failure(
            "merge",
            GitRemoteReason.CREDENTIAL_MISSING,
            extract_git_host(remote_url),
            req.workspace_path,
        )

    try:
        result = await promote_merge(
            req.workspace_path,
            source_branch=req.source_branch,
            target_branch=req.target_branch,
            remote=req.remote,
            auth=resolved.auth,
            host=resolved.host,
            message=req.message,
        )
    except ServiceError as exc:
        await _record_merge_event(
            req.workspace_path,
            target_env,
            req.user_id,
            succeeded=False,
            detail={
                "source_branch": req.source_branch,
                "target_branch": req.target_branch,
                "error_code": exc.code.value,
            },
        )
        raise

    await _record_merge_event(
        req.workspace_path,
        target_env,
        req.user_id,
        succeeded=True,
        commit_hash=result["merge_commit"],
        detail={
            "source_branch": req.source_branch,
            "source_environment": source_env,
            "target_branch": req.target_branch,
            "merged_commits": result["merged_commits"],
            "files_changed": result["files_changed"],
        },
    )

    result["source_environment"] = source_env
    result["target_environment"] = target_env
    return result


@router.post("/sync")
async def git_sync(req: GitSyncRequest):
    """Merge a base branch into THIS working copy's branch.

    The conflict-resolution path of a promotion: bring the target's newer
    commits into the source environment, resolve the conflicts in the editor,
    commit, then merge again. A conflicted outcome is reported as a successful
    start of that resolution (``conflicted: true``), not as an error — the
    conflicted files are now in the working tree, where they belong.

    INVALIDATES the context (the working tree changed).
    """
    from src.git.merge import sync_with_base
    from src.sqlmesh.env_locks import check_environment_lock

    _require_platform_branch(req.base_branch, "base branch")

    # A merge landing mid-apply would desynchronize the recorded applied commit
    # from the code that actually ran.
    await check_environment_lock(req.workspace_path, operation="sync")

    resolved, remote_url = await _resolve_remote_auth(
        req.user_id, req.workspace_path, req.remote
    )
    if not remote_url:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace has no '{req.remote}' remote. Bind a Git repository first.",
        )

    # A public repository can be fetched anonymously, so a missing credential is
    # not rejected up-front (unlike merge, which has to push).
    result = await sync_with_base(
        req.workspace_path,
        base_branch=req.base_branch,
        remote=req.remote,
        auth=resolved.auth,
        host=resolved.host,
        message=req.message,
    )

    # A branch switch retires the tree the sandbox's Context describes;
    # stop its children rather than refresh them, and drop the runtime's
    # cached metadata (same trade at this call site).
    get_executor_manager().invalidate(req.workspace_path)
    get_metadata_context().invalidate(req.workspace_path)

    from datacode_common.naming import env_of

    environment = env_of(result["branch"])
    if environment:
        await _record_merge_event(
            req.workspace_path,
            environment,
            req.user_id,
            succeeded=not result["conflicted"],
            commit_hash=result["merge_commit"],
            detail={
                "direction": "sync",
                "base_branch": req.base_branch,
                "conflicts": result["conflicts"],
                "up_to_date": result["up_to_date"],
            },
        )

    return result


@router.get("/log")
async def git_log(workspace_path: WorkspacePath, branch: str = "", limit: int = 50):
    """Return commit history (newest first). Timestamps are UTC ISO 8601."""
    from src.git.ops import run_git_async

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
    from src.git.ops import run_git_async

    result = await run_git_async(
        workspace_path, ["show", "--name-status", "--format=", commit_hash]
    )
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
    from src.git.ops import run_git_async

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
    from src.git.ops import (
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

    # Create remote repository under the token's own account.
    creation = await GitOperations.create_remote_repo(
        name=req.repo_name,
        token=cred.token,
        server_url=cred.server_url or "",
        provider=req.provider,
        private=req.is_private,
    )
    if not creation.ok or creation.info is None:
        raise _remote_failure(
            "publish",
            creation.reason,
            extract_git_host(cred.server_url or ""),
            req.workspace_path,
            creation.detail,
        )
    repo_info = creation.info

    auth = GitAuth(
        token=cred.token, provider=cred.provider or "", username=cred.username or ""
    )
    await asyncio.to_thread(
        GitOperations.add_remote, req.workspace_path, repo_info.clone_url
    )
    push_result = await push_async(req.workspace_path, "main", "origin", auth)
    if not push_result.ok:
        raise _remote_failure(
            "publish",
            push_result.reason,
            extract_git_host(repo_info.clone_url),
            req.workspace_path,
            push_result.detail,
        )

    return {
        "success": True,
        "remote_url": repo_info.clone_url,
        "html_url": repo_info.html_url,
        "message": f"Published to {repo_info.html_url}",
    }
