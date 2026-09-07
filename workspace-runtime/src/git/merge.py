"""Cross-branch merge orchestration for environment code promotion.

The platform splits what used to be one atomic "promote" into TWO user-visible
operations, because merging code and landing data have different failure modes
and different owners:

1. **Code merge** (this module): move commits from one environment branch onto
   another. Two primitives, mirroring the pull-request model:

   * :func:`promote_merge` — merge the source branch into the TARGET branch and
     publish the result on the remote. Computed with ``git merge-tree`` and
     pushed as a bare commit, so no member's working copy is touched. A conflict
     aborts it: nothing is written and the conflicted paths are returned,
     because a conflict needs a human with a working tree.
   * :func:`sync_with_base` — merge a base branch INTO the caller's own
     environment branch, inside their own working copy. This is where conflicts
     are meant to be resolved: they stay in the working tree and surface in the
     Git panel as ordinary ``U`` entries.

2. **Data and orchestration landing** (elsewhere): SQLMesh plan & apply on the
   target environment, then an explicit code-location reload.

Splitting them is what makes each step retryable, auditable and independently
authorizable, and it removes the need for a web three-way merge editor: the
resolution path is always "sync in the source environment, fix, commit, retry
the merge".

Every git invocation runs in the CALLER's validated working copy. That copy
shares its object database and its refs with the user's other worktrees, so
merge objects can be written and every ref read without materializing anything
new, and pushing ``<sha>:refs/heads/<branch>`` publishes the merge without
moving a single local branch.
"""

from __future__ import annotations

import asyncio
import typing as t

from src.git.ops import (
    GitAuth,
    GitMergeOperations,
    GitOperations,
    GitWorktreeOperations,
    fetch_async,
    is_valid_branch_name,
)
from src.utils.errors import (
    ConflictError,
    GitMergeConflictError,
    GitRemoteAccessError,
    InvalidRequestError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Upper bound on the commit list a preview returns: it answers "what am I about
# to promote", not "show me the whole history".
MAX_PREVIEW_COMMITS = 100


def _validate_branch(branch: str, field: str) -> str:
    """Reject a branch name that is unsafe to pass to git."""
    if not is_valid_branch_name(branch):
        raise InvalidRequestError(f"Invalid {field}: {branch!r}")
    return branch


def _resolve_ref_or_raise(
    workspace_path: str, branch: str, remote: str, field: str
) -> str:
    """Resolve a branch name to an existing ref, or explain why it cannot be."""
    ref = GitMergeOperations.resolve_ref(workspace_path, branch, remote)
    if not ref:
        raise ConflictError(
            f"Branch '{branch}' ({field}) is unknown locally and on '{remote}'. "
            f"Fetch the repository or push the branch first."
        )
    return ref


async def _fetch_or_raise(
    workspace_path: str,
    remote: str,
    auth: t.Optional[GitAuth],
    host: str,
    operation: str,
) -> None:
    """Refresh remote-tracking refs, failing loudly.

    A merge decided on stale refs is worse than no merge: it can promote code a
    teammate already superseded, or report a conflict that no longer exists.
    """
    result = await fetch_async(workspace_path, remote, True, auth)
    if not result.ok:
        raise GitRemoteAccessError(operation, result.reason, host, result.detail)


def default_merge_message(source_branch: str, target_branch: str) -> str:
    """Default merge commit subject, mirroring git's own wording."""
    return f"Merge branch '{source_branch}' into '{target_branch}'"


async def preview_merge(
    workspace_path: str,
    source_branch: str,
    target_branch: str,
    remote: str = "origin",
    auth: t.Optional[GitAuth] = None,
    file_path: str = "",
    limit: int = MAX_PREVIEW_COMMITS,
) -> t.Dict[str, t.Any]:
    """Describe what merging ``source_branch`` into ``target_branch`` would do.

    Read-only. Returns the commits, the changed files and — crucially — the
    conflict verdict from the real merge machinery (``git merge-tree``), so the
    user confirms a merge knowing whether it can land at all.

    When ``file_path`` is given, only that file's patch is returned, which is
    what backs opening a branch diff in the editor.

    A failed fetch is reported (``remote_synced: false``) rather than raised: a
    preview must still render for a member without a write credential, it just
    has to say that it may be stale.
    """
    _validate_branch(source_branch, "source branch")
    _validate_branch(target_branch, "target branch")

    # Best effort: a preview must still render when no credential is configured
    # (private repo, read-only member), it just says so.
    remote_synced = True
    fetch_result = await fetch_async(workspace_path, remote, True, auth)
    if not fetch_result.ok:
        remote_synced = False
        logger.warning(
            "git_branch_diff_fetch_failed",
            workspace=workspace_path,
            remote=remote,
            reason=fetch_result.reason,
        )

    source_ref = _resolve_ref_or_raise(
        workspace_path, source_branch, remote, "source"
    )
    target_ref = _resolve_ref_or_raise(
        workspace_path, target_branch, remote, "target"
    )

    if file_path:
        diff = await asyncio.to_thread(
            GitMergeOperations.diff_file_patch,
            workspace_path,
            target_ref,
            source_ref,
            file_path,
        )
        return {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "file_path": file_path,
            "diff": diff or "No textual diff available for this file.",
        }

    merge_base, commits, files, probe, unpushed = await asyncio.gather(
        asyncio.to_thread(
            GitMergeOperations.merge_base, workspace_path, target_ref, source_ref
        ),
        asyncio.to_thread(
            GitMergeOperations.list_commits,
            workspace_path,
            target_ref,
            source_ref,
            min(limit, MAX_PREVIEW_COMMITS),
        ),
        asyncio.to_thread(
            GitMergeOperations.diff_files, workspace_path, target_ref, source_ref
        ),
        asyncio.to_thread(
            GitMergeOperations.probe_merge, workspace_path, target_ref, source_ref
        ),
        asyncio.to_thread(
            _count_unpushed, workspace_path, source_branch, remote
        ),
    )

    return {
        "source_branch": source_branch,
        "target_branch": target_branch,
        # Resolved refs are reported so the user can tell a local-only branch
        # from the shared, remote-tracked one.
        "source_ref": source_ref,
        "target_ref": target_ref,
        "remote_synced": remote_synced,
        "merge_base": merge_base,
        "commits": commits,
        "commits_ahead": len(commits),
        "files": [
            {
                "path": entry.path,
                "status": entry.status,
                "original_path": entry.original_path,
            }
            for entry in files
        ],
        "conflicts": probe.conflicts,
        "can_merge": probe.clean and len(commits) > 0,
        "up_to_date": len(commits) == 0,
        # Commits the source branch holds locally but has not pushed. They would
        # NOT be promoted, so the UI warns instead of silently promoting less.
        "source_unpushed": unpushed,
    }


def _count_unpushed(workspace_path: str, branch: str, remote: str) -> int:
    """Commits a local branch holds that its remote-tracking ref does not.

    Zero when either side is absent: a branch that exists only on the remote has
    nothing un-pushed, and one that exists only locally is reported by the
    ref-resolution step instead.
    """
    local = f"refs/heads/{branch}"
    tracking = f"refs/remotes/{remote}/{branch}"
    if not GitWorktreeOperations.ref_exists(workspace_path, local):
        return 0
    if not GitWorktreeOperations.ref_exists(workspace_path, tracking):
        return 0
    return GitMergeOperations.count_commits(workspace_path, tracking, local)


async def promote_merge(
    workspace_path: str,
    source_branch: str,
    target_branch: str,
    remote: str = "origin",
    auth: t.Optional[GitAuth] = None,
    host: str = "",
    message: str = "",
) -> t.Dict[str, t.Any]:
    """Merge ``source_branch`` into ``target_branch`` and publish it.

    Refuses rather than guesses in every ambiguous case: unknown branch,
    un-pushed source commits, nothing to merge, or conflicts. Nothing is written
    anywhere until the merge is proven clean, and the only mutation is a push of
    the new merge commit — the target environment picks it up with its own
    explicit pull + reload, which is the second half of the promotion.
    """
    _validate_branch(source_branch, "source branch")
    _validate_branch(target_branch, "target branch")
    if source_branch == target_branch:
        raise InvalidRequestError("Source and target branch must differ")

    await _fetch_or_raise(workspace_path, remote, auth, host, "merge")

    source_ref = _resolve_ref_or_raise(
        workspace_path, source_branch, remote, "source"
    )
    target_ref = _resolve_ref_or_raise(
        workspace_path, target_branch, remote, "target"
    )

    # The promotion publishes REMOTE state, so local-only commits would be
    # silently left behind. Say so instead.
    unpushed = await asyncio.to_thread(
        _count_unpushed, workspace_path, source_branch, remote
    )
    if unpushed:
        raise ConflictError(
            f"Branch '{source_branch}' has {unpushed} commit(s) that are not "
            f"pushed yet. Push them first so the merge promotes the code you see."
        )

    commits, files = await asyncio.gather(
        asyncio.to_thread(
            GitMergeOperations.list_commits,
            workspace_path,
            target_ref,
            source_ref,
            MAX_PREVIEW_COMMITS,
        ),
        asyncio.to_thread(
            GitMergeOperations.diff_files, workspace_path, target_ref, source_ref
        ),
    )
    if not commits:
        raise ConflictError(
            f"Branch '{target_branch}' already contains every commit of "
            f"'{source_branch}'. There is nothing to merge."
        )

    probe = await asyncio.to_thread(
        GitMergeOperations.probe_merge, workspace_path, target_ref, source_ref
    )
    if not probe.ok:
        raise ConflictError(
            f"The merge of '{source_branch}' into '{target_branch}' could not be "
            f"computed: {probe.detail}"
        )
    if probe.conflicts:
        raise GitMergeConflictError(
            source_branch=source_branch,
            target_branch=target_branch,
            conflicts=probe.conflicts,
        )

    subject = message.strip() or default_merge_message(source_branch, target_branch)
    merge_commit = await asyncio.to_thread(
        GitMergeOperations.merge_in_bare,
        workspace_path,
        target_ref,
        source_ref,
        subject,
        probe.tree,
    )
    if not merge_commit:
        raise ConflictError(
            f"The merge commit for '{target_branch}' could not be created. "
            f"Retry once the repository is reachable."
        )

    push = await asyncio.to_thread(
        GitMergeOperations.push_commit,
        workspace_path,
        merge_commit,
        target_branch,
        remote,
        auth,
    )
    if not push.ok:
        raise GitRemoteAccessError("merge", push.reason, host, push.detail)

    logger.info(
        "git_branch_merged",
        workspace=workspace_path,
        source=source_branch,
        target=target_branch,
        commit=merge_commit[:8],
        commit_count=len(commits),
    )
    return {
        "success": True,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "merge_commit": merge_commit,
        "merged_commits": len(commits),
        "files_changed": len(files),
    }


async def sync_with_base(
    workspace_path: str,
    base_branch: str,
    remote: str = "origin",
    auth: t.Optional[GitAuth] = None,
    host: str = "",
    message: str = "",
) -> t.Dict[str, t.Any]:
    """Merge ``base_branch`` into the branch checked out at ``workspace_path``.

    The counterpart of :func:`promote_merge`: it brings the target's newer
    commits into the caller's own environment BEFORE they promote, which is how
    a conflict gets resolved in the one place a conflict can be resolved — a
    working copy, with an editor.

    Tracked local modifications are refused up-front: git would abort the merge
    anyway, and a half-started merge over dirty files is the worst state to hand
    a user. An unresolved earlier merge is caught by the same check, because its
    conflicted paths are uncommitted changes too.
    """
    _validate_branch(base_branch, "base branch")

    current_branch = await asyncio.to_thread(
        GitOperations.current_branch, workspace_path
    )
    if not current_branch:
        raise ConflictError(
            "This working copy has no branch checked out, so it cannot be synced."
        )
    if current_branch == base_branch:
        raise InvalidRequestError(
            f"Branch '{base_branch}' is already the checked-out branch"
        )

    # Untracked files ("?") never block a merge, so they are not counted.
    changes = await asyncio.to_thread(GitOperations.list_changes, workspace_path)
    dirty = [change.path for change in changes if change.status != "?"]
    if dirty:
        unresolved = [change.path for change in changes if change.status == "U"]
        if unresolved:
            raise ConflictError(
                f"A previous merge is still unresolved in {len(unresolved)} file(s). "
                f"Resolve and commit them before syncing again."
            )
        raise ConflictError(
            f"This working copy has {len(dirty)} uncommitted change(s). "
            f"Commit or discard them before syncing with '{base_branch}'."
        )

    await _fetch_or_raise(workspace_path, remote, auth, host, "sync")
    base_ref = _resolve_ref_or_raise(workspace_path, base_branch, remote, "base")

    subject = message.strip() or default_merge_message(base_branch, current_branch)
    result = await asyncio.to_thread(
        GitMergeOperations.merge_in_worktree, workspace_path, base_ref, subject
    )
    if not result.ok:
        raise ConflictError(
            f"Syncing with '{base_branch}' failed: {result.detail}"
        )

    logger.info(
        "git_branch_synced",
        workspace=workspace_path,
        branch=current_branch,
        base=base_branch,
        conflicts=len(result.conflicts),
        up_to_date=result.already_up_to_date,
    )
    return {
        "success": True,
        "branch": current_branch,
        "base_branch": base_branch,
        # A conflicted sync is a SUCCESSFUL start of a resolution, not an error:
        # the conflicted files are now in the working tree, waiting for the user.
        "conflicted": bool(result.conflicts),
        "conflicts": result.conflicts,
        "merge_commit": result.merge_commit,
        "up_to_date": result.already_up_to_date,
    }
