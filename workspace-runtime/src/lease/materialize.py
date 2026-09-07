"""Working-copy materialization — turning a lease into files (design 06 P4).

There is exactly ONE materialization path, and this module is it:

    git clone --bare  ->  {user_root}/.repo   (shared object database)
    git worktree add  ->  {user_root}/envs/{env}   (the working copy)

The medium under ``materialize.root`` is a deployment concern
(:mod:`datacode_common.materialize`): this code only ever sees a writable
directory and always derives content from Git. That is what makes the medium
switchable with no code branch, and therefore what makes the P4b rollback a
config change (design 07 ADR-003 Q7).

Idempotence is the contract, not an optimization
------------------------------------------------
Every request for a leased workspace calls :func:`materialize`, so the common
case is "already materialized". It must therefore be cheap and side-effect free
when the working copy is present and valid. It must also be *correct* when the
previous attempt died halfway: a bare clone interrupted by an OOMKill leaves a
directory that looks like ``.repo`` but has no ``HEAD``. Treating presence as
validity would then wedge the workspace permanently, because nothing would ever
retry the clone. Hence the explicit validity checks below rather than
``path.exists()``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import typing as t
from dataclasses import dataclass
from pathlib import Path

from datacode_common.naming import branch_of, split_lease_key
from datacode_common.workspace_paths import REPO_DIR, envs_root, repo_path, user_root_path

from src.git.ops import GitAuth, GitOperations, GitWorktreeOperations
from src.utils.logging import get_logger

logger = get_logger(__name__)


class MaterializationError(RuntimeError):
    """The working copy could not be produced from Git.

    Always attributable to a cause the caller can report: an unreachable remote,
    a missing branch, a full disk. Never used for "already materialized".
    """


@dataclass(frozen=True)
class MaterializedCopy:
    """Where a lease's files live, once they exist."""

    lease_key: str
    user_root: str
    repo_path: str
    workspace_path: str
    branch: str
    # False when the copy was already present, so callers can report first-hit
    # latency without timing every request.
    freshly_materialized: bool


def is_valid_bare_repo(path: str) -> bool:
    """Whether ``path`` is a usable bare object database.

    ``HEAD`` is the marker: ``git clone --bare`` writes it last enough that a
    directory without it is an interrupted clone, not a repository. Checking the
    directory's existence instead would make a half-written clone permanent.
    """
    return os.path.isfile(os.path.join(path, "HEAD"))


def is_valid_worktree(path: str) -> bool:
    """Whether ``path`` is an attached worktree rather than a stray directory.

    A linked worktree has a ``.git`` FILE (a gitdir pointer), not a directory.
    A plain directory at that path means a previous attempt failed after
    ``mkdir`` but before ``worktree add``.
    """
    return os.path.isfile(os.path.join(path, ".git"))


def materialize(
    *,
    materialize_root: str,
    lease_key: str,
    repo_url: str = "",
    auth: t.Optional[GitAuth] = None,
    shallow_depth: int = 0,
    fetch_if_present: bool = False,
) -> MaterializedCopy:
    """Ensure the working copy for ``lease_key`` exists, and return where.

    Preconditions: the caller HOLDS the lease for ``lease_key`` (see
    :mod:`src.lease.holder`). This function does not take the lease itself,
    because materializing without ownership is precisely the double-writer bug
    the lease exists to prevent — making it impossible to call safely by
    accident is better than checking here and hoping callers pass the truth.

    Args:
        repo_url: remote to clone from on a cache miss. May be empty only when
            ``.repo`` already exists, i.e. for a repository that was bound
            earlier; an empty URL with no local objects is a caller bug.
        fetch_if_present: refresh remote-tracking refs even on a cache hit.
            Off by default: fetching on every request would put a network round
            trip on the hot path of an already-warm workspace.

    Raises:
        MaterializationError: the copy could not be produced.
    """
    tenant_id, workspace_id, user_id, environment = split_lease_key(lease_key)
    branch = branch_of(environment)

    root = str(Path(materialize_root).expanduser())
    user_root = user_root_path(root, tenant_id, workspace_id, user_id)
    repo = repo_path(root, tenant_id, workspace_id, user_id)
    workspace_path = str(Path(envs_root(root, tenant_id, workspace_id, user_id)) / environment)

    fresh = False

    # ── 1. the shared object database ────────────────────────────────
    if not is_valid_bare_repo(repo):
        _discard_partial(repo, reason="invalid_bare_repo")
        if not repo_url:
            raise MaterializationError(
                f"no object database at {repo} and no repo_url to clone from; "
                f"workspace {workspace_id!r} appears not to be bound to a remote"
            )
        os.makedirs(user_root, exist_ok=True)
        result = GitOperations.clone_bare(
            repo_url, repo, auth=auth, depth=shallow_depth
        )
        if not result.ok:
            # Leave nothing behind: a partial .repo would be taken for valid on
            # the next attempt only if it happened to have HEAD, and would
            # otherwise accumulate.
            _discard_partial(repo, reason="clone_failed")
            raise MaterializationError(
                f"cannot clone {workspace_id!r} objects: {result.detail or result.reason}"
            )
        fresh = True
        logger.info(
            "materialize_repo_cloned",
            lease_key=lease_key,
            repo=repo,
            shallow_depth=shallow_depth,
        )
    elif fetch_if_present:
        # Best-effort: stale refs degrade freshness, not correctness, and the
        # user can still work offline on what is already local.
        result = GitOperations.fetch_bare(repo, auth=auth)
        if not result.ok:
            logger.warning(
                "materialize_fetch_failed",
                lease_key=lease_key,
                detail=result.detail or result.reason,
            )

    # ── 2. the environment worktree ──────────────────────────────────
    if not is_valid_worktree(workspace_path):
        _discard_partial(workspace_path, reason="invalid_worktree")
        # Drop administrative records pointing at directories that no longer
        # exist, otherwise git refuses to re-add a worktree it still believes
        # is registered — the exact state an `ephemeral` medium produces on
        # every pod restart.
        GitWorktreeOperations.worktree_prune(repo)
        os.makedirs(envs_root(root, tenant_id, workspace_id, user_id), exist_ok=True)

        result = _add_worktree(repo, workspace_path, branch)
        if not result.ok:
            _discard_partial(workspace_path, reason="worktree_add_failed")
            raise MaterializationError(
                f"cannot materialize {environment!r} of {workspace_id!r}: "
                f"{result.detail or result.reason}"
            )
        fresh = True
        logger.info(
            "materialize_worktree_added",
            lease_key=lease_key,
            workspace_path=workspace_path,
            branch=branch,
        )

    return MaterializedCopy(
        lease_key=lease_key,
        user_root=user_root,
        repo_path=repo,
        workspace_path=workspace_path,
        branch=branch,
        freshly_materialized=fresh,
    )


def _add_worktree(repo: str, workspace_path: str, branch: str):
    """Attach ``branch`` as a worktree, choosing the right start point.

    Three cases, and the order matters:

    1. **The branch already exists locally.** Either the platform materialized
       it before, or the user created it. Attach directly — passing a start
       point would be an error.
    2. **The branch exists on the remote.** Create it FROM the remote-tracking
       ref with ``--track``, so pull/push need no arguments afterwards. This is
       the case that only works because
       :meth:`GitWorktreeOperations.normalize_bare_clone` moved remote state out
       of ``refs/heads/*``; without it every remote branch would look like case
       1 and silently get no upstream.
    3. **The branch exists nowhere.** A brand-new environment: branch from the
       default branch's tip. Normal, not an error.
    """
    if GitWorktreeOperations.local_branch_exists(repo, branch):
        return GitWorktreeOperations.worktree_add(repo, workspace_path, branch)

    if GitWorktreeOperations.remote_branch_exists(repo, branch):
        return GitWorktreeOperations.worktree_add(
            repo,
            workspace_path,
            branch,
            start_point=f"refs/remotes/origin/{branch}",
            track_remote=True,
        )

    return GitWorktreeOperations.worktree_add(
        repo, workspace_path, branch, start_point="HEAD"
    )


def discard(materialize_root: str, lease_key: str) -> None:
    """Remove a lease's working copy, keeping the shared object database.

    Used by LRU reclamation: the worktree is the disposable part, while
    ``.repo`` may serve this user's other environments and is expensive to
    re-fetch. Uncommitted work is protected by the WIP snapshot mechanism
    (design 07 Q6), not by this function — callers must snapshot first.
    """
    tenant_id, workspace_id, user_id, environment = split_lease_key(lease_key)
    root = str(Path(materialize_root).expanduser())
    repo = repo_path(root, tenant_id, workspace_id, user_id)
    workspace_path = str(
        Path(envs_root(root, tenant_id, workspace_id, user_id)) / environment
    )

    if is_valid_worktree(workspace_path):
        GitWorktreeOperations.worktree_remove(repo, workspace_path, force=True)
    _discard_partial(workspace_path, reason="discarded")
    GitWorktreeOperations.worktree_prune(repo)
    logger.info("materialize_discarded", lease_key=lease_key)


def _discard_partial(path: str, *, reason: str) -> None:
    """Delete a leftover path, tolerating its absence.

    Only ever called on a path the layout functions produced (never on caller
    input), so this cannot be pointed at an arbitrary directory.
    """
    if not os.path.exists(path):
        return
    if os.path.basename(path) == REPO_DIR or os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    logger.info("materialize_partial_discarded", path=path, reason=reason)
