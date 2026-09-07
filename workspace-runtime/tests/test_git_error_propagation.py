"""Git failure propagation: the remote's own words must reach the caller.

The defect these tests pin down, end to end:

A workspace bound to a repository that does not exist produced
``Git operation failed: add worktree for environment prod`` — a report naming
neither the repository nor the reason, while the runtime's own log already said
``repo_not_found`` / ``remote: Repository not found``. Three separate losses
stacked up:

1. ``ensure_repository`` logged the fetch failure and continued, so the request
   died later at an operation that knew nothing about the cause;
2. the surviving error carried its detail as a bare string, so the reason code
   never reached the frontend's Git parsing path;
3. the control plane rebuilt the message from the upstream one, prefixing it a
   second time.

Each is asserted independently below, so a regression names which link broke.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.git_reasons import GitRemoteReason  # noqa: E402

from src.environments.git_binder import (  # noqa: E402
    _TERMINAL_FETCH_REASONS,
    GitBinder,
)
from src.git.ops import GitCommandResult, GitOperations  # noqa: E402
from src.utils.errors import GitOperationError, GitRemoteAccessError  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for git-binder tests"
)

_TENANT = "t1"
_WORKSPACE = "ws-t1-shop"
_USER = "u1"
_REMOTE = "https://github.com/BiyoTech/loganhu-test.git"

# The stderr GitHub actually returns for a missing repository, verbatim from the
# report that motivated this work.
_REPO_NOT_FOUND_STDERR = (
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/BiyoTech/loganhu-test.git/' not found\n"
)


@pytest.fixture()
def binder(tmp_path):
    return GitBinder(str(tmp_path))


@pytest.fixture(autouse=True)
def _no_credential_lookup(monkeypatch):
    """Credential resolution needs a database; the token is irrelevant here."""
    async def _auth(self, user_id: str, git_repo_url: str):
        return None

    monkeypatch.setattr(GitBinder, "_auth_for", _auth)


def _fetch_returning(reason: str, detail: str = ""):
    def _fetch(repo, remote="origin", prune=True, auth=None):
        return GitCommandResult(ok=False, reason=reason, detail=detail)

    return _fetch


# ── Link 1: terminal fetch failures are raised, not logged away ─────────────


def test_repo_not_found_surfaces_with_the_remote_wording(binder, monkeypatch):
    """The whole point: the user reads "Repository not found", not "add worktree"."""
    monkeypatch.setattr(
        GitOperations,
        "fetch",
        staticmethod(
            _fetch_returning(GitRemoteReason.REPO_NOT_FOUND, _REPO_NOT_FOUND_STDERR)
        ),
    )

    with pytest.raises(GitRemoteAccessError) as excinfo:
        import asyncio

        asyncio.run(
            binder.ensure_repository(
                tenant_id=_TENANT,
                workspace_id=_WORKSPACE,
                user_id=_USER,
                git_repo_url=_REMOTE,
                bootstrap_branch="main",
            )
        )

    error = excinfo.value
    assert error.status_code == 422
    assert error.detail["git_error_reason"] == GitRemoteReason.REPO_NOT_FOUND
    assert error.detail["git_operation"] == "fetch"
    assert error.detail["git_remote_host"] == "github.com"
    # The remote's own sentence is carried, not summarized away.
    assert "Repository not found" in error.detail["git_detail"]
    assert "loganhu-test.git" in error.detail["git_detail"]


@pytest.mark.parametrize(
    "reason",
    sorted(_TERMINAL_FETCH_REASONS),
)
def test_every_terminal_reason_is_raised(binder, monkeypatch, reason):
    monkeypatch.setattr(
        GitOperations, "fetch", staticmethod(_fetch_returning(reason, "boom"))
    )
    import asyncio

    with pytest.raises(GitRemoteAccessError) as excinfo:
        asyncio.run(
            binder.ensure_repository(
                tenant_id=_TENANT,
                workspace_id=_WORKSPACE,
                user_id=_USER,
                git_repo_url=_REMOTE,
                bootstrap_branch="main",
            )
        )
    assert excinfo.value.detail["git_error_reason"] == reason


@pytest.mark.parametrize(
    "reason", [GitRemoteReason.HOST_UNREACHABLE, GitRemoteReason.UNKNOWN]
)
def test_transient_reasons_keep_the_stale_mirror_usable(binder, monkeypatch, reason):
    """A network blip must not block opening a copy that is already on disk."""
    monkeypatch.setattr(
        GitOperations, "fetch", staticmethod(_fetch_returning(reason, "timeout"))
    )
    import asyncio

    repo = asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=_REMOTE,
            bootstrap_branch="main",
        )
    )
    assert Path(repo).is_dir()


def test_terminal_and_transient_reasons_are_disjoint_and_deliberate():
    """A reason in neither set silently becomes transient — state the intent."""
    assert GitRemoteReason.HOST_UNREACHABLE not in _TERMINAL_FETCH_REASONS
    assert GitRemoteReason.UNKNOWN not in _TERMINAL_FETCH_REASONS
    assert GitRemoteReason.REPO_NOT_FOUND in _TERMINAL_FETCH_REASONS


# ── Link 2: the reason survives inside a structured detail ──────────────────


def test_git_operation_error_inlines_the_reason_for_the_frontend():
    """A bare-string detail is invisible to the frontend's Git parsing path."""
    error = GitOperationError(
        "add worktree for environment prod",
        detail="branch 'main' exists neither locally nor on origin",
        reason=GitRemoteReason.BRANCH_NOT_FOUND,
        host="github.com",
    )
    body = error.to_response_dict()

    assert body["error_code"] == "GIT_OPERATION_FAILED"
    # Inlined at the TOP level: that is where the frontend reads them.
    assert body["git_error_reason"] == GitRemoteReason.BRANCH_NOT_FOUND
    assert body["git_remote_host"] == "github.com"
    assert "exists neither locally" in body["git_detail"]
    # The authoritative message is never clobbered by the inlined payload.
    assert body["message"].startswith("Git operation failed:")


def test_git_operation_error_defaults_to_unknown_not_to_a_missing_key():
    """Callers that know no reason must still produce a parseable body."""
    body = GitOperationError("init bare repository", detail="/tmp/x").to_response_dict()
    assert body["git_error_reason"] == GitRemoteReason.UNKNOWN


def test_worktree_failure_carries_the_underlying_reason(binder, monkeypatch):
    """`add worktree` failures name the Git reason instead of only themselves."""
    import asyncio

    asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            bootstrap_branch="main",
        )
    )

    with pytest.raises(GitOperationError) as excinfo:
        asyncio.run(
            binder.ensure_worktree(
                tenant_id=_TENANT,
                workspace_id=_WORKSPACE,
                user_id=_USER,
                environment="dev",
                branch="env/dev",
                # No start point and no such branch anywhere: BRANCH_NOT_FOUND.
            )
        )
    assert excinfo.value.detail["git_error_reason"] == GitRemoteReason.BRANCH_NOT_FOUND


# ── Rebinding: disk must never outvote the registry ────────────────────────


def _init_remote(path: Path) -> str:
    """Create a real remote repository with one commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True,
                   capture_output=True)
    work = path.parent / f"{path.name}-work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True,
                   capture_output=True)
    (work / "marker.txt").write_text(path.name, encoding="utf-8")
    for args in (
        ["add", "."],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        ["remote", "add", "origin", str(path)],
        ["push", "origin", "main"],
    ):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    return str(path)


def test_rebinding_to_another_remote_repoints_and_prunes(binder, tmp_path):
    """DB said repo B while disk kept pointing at repo A — silently, no error.

    Repointing plus ``fetch --prune`` is the fix that is safe in every caller:
    the old remote-tracking refs disappear, so branch resolution can no longer
    reach the previous repository. The local head is retained on purpose (see
    ``_repoint_remote``): the supported rebinding path pushes it to the new
    remote, so deleting it would destroy the commits about to be published.
    """
    import asyncio

    remote_a = _init_remote(tmp_path / "remote-a.git")
    remote_b = _init_remote(tmp_path / "remote-b.git")

    repo = asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=remote_a,
            bootstrap_branch="main",
        )
    )
    asyncio.run(
        binder.ensure_worktree(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            environment="prod",
            branch="main",
        )
    )
    assert GitOperations.remote_url(repo) == remote_a
    commit_a = _rev_parse(repo, "refs/remotes/origin/main")

    asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=remote_b,
            bootstrap_branch="main",
        )
    )
    assert GitOperations.remote_url(repo) == remote_b
    # The mirror now answers for remote B, so nothing resolves to A anymore.
    commit_b = _rev_parse(repo, "refs/remotes/origin/main")
    assert commit_b and commit_b != commit_a


def _rev_parse(repo: str, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_unchanged_remote_keeps_the_local_branch(binder, tmp_path):
    """Re-opening must be a no-op, not a branch reset on every request."""
    import asyncio

    remote = _init_remote(tmp_path / "remote.git")
    for _ in range(2):
        repo = asyncio.run(
            binder.ensure_repository(
                tenant_id=_TENANT,
                workspace_id=_WORKSPACE,
                user_id=_USER,
                git_repo_url=remote,
                bootstrap_branch="main",
            )
        )
        asyncio.run(
            binder.ensure_worktree(
                tenant_id=_TENANT,
                workspace_id=_WORKSPACE,
                user_id=_USER,
                environment="prod",
                branch="main",
            )
        )

    from src.git.ops import GitWorktreeOperations

    assert GitWorktreeOperations.local_branch_exists(repo, "main")


# ── Empty remote: a worktree without a scaffold ─────────────────────────────


def test_empty_remote_gets_a_bootstrap_commit_only_when_allowed(binder, tmp_path):
    """Git cannot attach a worktree to an unborn branch; nothing is scaffolded."""
    import asyncio

    empty = tmp_path / "empty.git"
    empty.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(empty)], check=True,
                   capture_output=True)

    # Without the flag the branch stays unborn: the caller has not stated that
    # the remote is legitimately empty, so no history is invented.
    repo = asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=str(empty),
            bootstrap_branch="main",
        )
    )
    from src.git.ops import GitWorktreeOperations

    assert not GitWorktreeOperations.local_branch_exists(repo, "main")

    repo = asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=str(empty),
            bootstrap_branch="main",
            allow_bootstrap_commit=True,
        )
    )
    assert GitWorktreeOperations.local_branch_exists(repo, "main")

    materialization = asyncio.run(
        binder.ensure_worktree(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            environment="prod",
            branch="main",
        )
    )
    workspace_path = Path(materialization.workspace_path)
    assert workspace_path.is_dir()
    # The reuse contract: an empty copy, NOT a platform template.
    tracked = [p.name for p in workspace_path.iterdir() if p.name != ".git"]
    assert tracked == [], f"an empty remote must not be scaffolded, found {tracked}"


def test_bootstrap_commit_is_not_seeded_when_the_remote_has_a_branch(
    binder, tmp_path
):
    """Seeding a second root commit would fork an existing history."""
    import asyncio

    remote = _init_remote(tmp_path / "remote.git")
    repo = asyncio.run(
        binder.ensure_repository(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            git_repo_url=remote,
            bootstrap_branch="main",
            allow_bootstrap_commit=True,
        )
    )
    from src.git.ops import GitWorktreeOperations

    # No local head was invented: the mirror already answers for `main`.
    assert not GitWorktreeOperations.local_branch_exists(repo, "main")

    materialization = asyncio.run(
        binder.ensure_worktree(
            tenant_id=_TENANT,
            workspace_id=_WORKSPACE,
            user_id=_USER,
            environment="prod",
            branch="main",
        )
    )
    # The remote's file is there, i.e. the existing code was reused.
    assert (Path(materialization.workspace_path) / "marker.txt").is_file()
