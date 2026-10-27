"""Branch reuse and preservation, against real git.

A destroy releases only what the platform owns; the remote branch survives. The
consequence is a creation-side contract that has to be exercised with a real
repository, because it is entirely about what ``git worktree add`` picks as its
start point:

* recreating an environment whose remote branch still exists RESUMES that
  branch — the user's own commits, not a fresh copy of the base;
* a stale local branch left behind by an interrupted destroy is fast-forwarded
  onto the remote, because otherwise git would silently check out the old code;
* a local branch holding UNPUSHED commits is kept as-is: those commits exist
  nowhere else, and tidying them away would destroy the very thing this whole
  contract protects.

The fixtures build a real bare "remote" on disk, so ``origin`` behaves exactly
as it does in production minus the network.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.environments.git_binder import (  # noqa: E402
    BRANCH_SOURCE_ADOPTED_LOCAL,
    BRANCH_SOURCE_CREATED_FROM_BASE,
    BRANCH_SOURCE_REUSED_REMOTE,
    GitBinder,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for branch reuse tests"
)

_TENANT = "t1"
_WORKSPACE = "ws-t1-shop"
_USER = "u1"
_ENV = "dev"
_BRANCH = "env/dev"


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _commit(path, filename: str, content: str, message: str) -> str:
    (Path(path) / filename).write_text(content)
    _git(path, "add", filename)
    _git(
        path,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        message,
    )
    return _git(path, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def remote(tmp_path) -> str:
    """A bare repository standing in for the team's origin, seeded on main."""
    remote_path = tmp_path / "remote.git"
    remote_path.mkdir()
    _git(remote_path, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _commit(seed, "base.sql", "SELECT 1", "base")
    _git(seed, "remote", "add", "origin", str(remote_path))
    _git(seed, "push", "origin", "main")
    return str(remote_path)


@pytest.fixture()
def binder(tmp_path) -> GitBinder:
    base = tmp_path / "base"
    base.mkdir()
    return GitBinder(str(base))


async def _materialize(binder: GitBinder, remote: str, *, start_branch="main"):
    """Run the production path: ensure the clone, then the worktree."""
    await binder.ensure_repository(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        git_repo_url=remote,
        bootstrap_branch="main",
    )
    return await binder.ensure_worktree(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
        branch=_BRANCH,
        start_branch=start_branch,
    )


def _publish_environment_branch(tmp_path, remote: str, content: str) -> str:
    """Push an ``env/dev`` branch to the remote, as a previous life would have."""
    clone = tmp_path / "publisher"
    _git(tmp_path, "clone", remote, str(clone))
    _git(clone, "checkout", "-b", _BRANCH)
    commit = _commit(clone, "user_model.sql", content, "user work")
    _git(clone, "push", "origin", _BRANCH)
    return commit


# ── Fresh creation ──────────────────────────────────────────────────────────


async def test_new_environment_branches_from_its_base(binder, remote):
    materialization = await _materialize(binder, remote)

    assert materialization.branch_source == BRANCH_SOURCE_CREATED_FROM_BASE
    # The base's content, and nothing of any other line of development.
    copy = Path(materialization.workspace_path)
    assert (copy / "base.sql").exists()
    assert not (copy / "user_model.sql").exists()


# ── Reuse of a preserved remote branch ──────────────────────────────────────


async def test_recreated_environment_resumes_the_remote_branch(
    binder, remote, tmp_path
):
    """The point of preserving the branch: the user gets their code back."""
    published = _publish_environment_branch(tmp_path, remote, "SELECT 'mine'")

    materialization = await _materialize(binder, remote)

    assert materialization.branch_source == BRANCH_SOURCE_REUSED_REMOTE
    assert materialization.commit == published
    copy = Path(materialization.workspace_path)
    assert (copy / "user_model.sql").read_text() == "SELECT 'mine'"


async def test_reused_branch_tracks_its_remote(binder, remote, tmp_path):
    """Tracking is what makes a later pull/push work without arguments — and
    what tells git this is the same line of development, not a namesake."""
    _publish_environment_branch(tmp_path, remote, "SELECT 'mine'")

    materialization = await _materialize(binder, remote)

    upstream = _git(
        materialization.workspace_path,
        "rev-parse",
        "--abbrev-ref",
        f"{_BRANCH}@{{upstream}}",
        check=False,
    )
    assert upstream.stdout.strip() == f"origin/{_BRANCH}"


# ── A stale local branch must not shadow the remote ─────────────────────────


async def test_stale_local_branch_is_fast_forwarded_to_the_remote(
    binder, remote, tmp_path
):
    """Git attaches a worktree to an existing LOCAL branch in preference to the
    remote, so a local head left behind by a failed destroy would serve old
    code while the team's work sat on origin."""
    # First life: create the environment and publish one commit.
    first = await _materialize(binder, remote)
    _commit(first.workspace_path, "user_model.sql", "SELECT 1", "first")
    _git(first.workspace_path, "push", "-u", "origin", _BRANCH)

    # Destroy removed the worktree but the local branch survived (the delete is
    # best-effort), while a teammate advanced the remote meanwhile.
    await binder.remove_worktree(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
    )
    advanced = _publish_extra_commit(tmp_path, remote)

    second = await _materialize(binder, remote)

    assert second.branch_source == BRANCH_SOURCE_REUSED_REMOTE
    assert second.commit == advanced
    assert (Path(second.workspace_path) / "teammate.sql").exists()


async def test_local_branch_with_unpushed_commits_is_kept(binder, remote, tmp_path):
    """Those commits exist nowhere else. Overwriting them to match the remote
    would lose code — the divergence is reported instead."""
    first = await _materialize(binder, remote)
    _commit(first.workspace_path, "user_model.sql", "SELECT 1", "pushed")
    _git(first.workspace_path, "push", "-u", "origin", _BRANCH)
    local_only = _commit(
        first.workspace_path, "wip.sql", "SELECT 2", "never pushed"
    )

    await binder.remove_worktree(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
    )
    _publish_extra_commit(tmp_path, remote)

    second = await _materialize(binder, remote)

    assert second.branch_source == BRANCH_SOURCE_ADOPTED_LOCAL
    assert second.commit == local_only
    assert (Path(second.workspace_path) / "wip.sql").exists()


def _publish_extra_commit(tmp_path, remote: str) -> str:
    """Advance ``env/dev`` on the remote, as another member would."""
    clone = tmp_path / f"teammate-{Path(tmp_path).name}"
    _git(tmp_path, "clone", "-b", _BRANCH, remote, str(clone))
    commit = _commit(clone, "teammate.sql", "SELECT 3", "teammate work")
    _git(clone, "push", "origin", _BRANCH)
    return commit


# ── Pending work ────────────────────────────────────────────────────────────


async def test_pending_work_counts_unpushed_commits(binder, remote):
    """The number the delete dialog warns with: committed locally, absent from
    the remote, and therefore the only code a destroy can still lose."""
    materialization = await _materialize(binder, remote)
    _commit(materialization.workspace_path, "a.sql", "SELECT 1", "one")
    _git(materialization.workspace_path, "push", "-u", "origin", _BRANCH)
    _commit(materialization.workspace_path, "b.sql", "SELECT 2", "two")
    _commit(materialization.workspace_path, "c.sql", "SELECT 3", "three")

    work = await binder.pending_work(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
        branch=_BRANCH,
    )

    assert work.unpushed == 2
    assert work.uncommitted == 0


async def test_pending_work_reports_zero_for_a_never_pushed_branch(binder, remote):
    """Without a tracking ref there is nothing to compare against. Reporting
    the whole history as "about to be lost" would make every first-push case
    look like an emergency."""
    await _materialize(binder, remote)

    work = await binder.pending_work(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
        branch=_BRANCH,
    )

    assert work.unpushed == 0


async def test_pending_work_counts_uncommitted_changes(binder, remote):
    materialization = await _materialize(binder, remote)
    (Path(materialization.workspace_path) / "draft.sql").write_text("SELECT 1")

    work = await binder.pending_work(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id=_USER,
        environment=_ENV,
        branch=_BRANCH,
    )

    assert work.uncommitted == 1


# ── The capability itself is gone ───────────────────────────────────────────


def test_binder_cannot_delete_a_remote_branch(binder):
    """Structural: the promise "the platform never deletes a remote branch" is
    verifiable only while no method can express it."""
    assert not [
        name for name in dir(binder) if "delete" in name and "remote" in name
    ]
