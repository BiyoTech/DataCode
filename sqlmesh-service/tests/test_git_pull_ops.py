"""Tests for the branch integration behind ``POST /api/v1/git/pull`` (real git).

The platform does NOT run ``git pull``: since Git 2.27 that command refuses to
run on a divergent branch unless ``pull.rebase`` is configured, which made every
environment branch with both local and remote commits unpullable, and made the
outcome depend on ambient machine configuration. ``GitOperations.pull``
therefore fetches explicitly and decides the strategy itself.

What is asserted here is exactly that contract:

* a branch that only fell behind fast-forwards (no merge commit appears);
* a DIVERGENT branch is integrated with a real merge commit — the case that used
  to fail with "Need to specify how to reconcile divergent branches";
* a conflicting integration is a SUCCESS whose conflicts stay in the working
  tree, where the user resolves them;
* uncommitted tracked work blocks the integration before anything is written;
* an unchanged branch reports ``up_to_date`` so the caller can skip invalidating
  the SQLMesh context.
"""

import os
import shutil
import subprocess

import pytest

from src.workspace.git_ops import (
    PULL_BLOCKED,
    PULL_CONFLICTED,
    PULL_FAST_FORWARD,
    PULL_MERGED,
    PULL_UP_TO_DATE,
    GitOperations,
)
from src.workspace.git_ops import GitWorktreeOperations as W

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable is required"
)


def _git(cwd: str, *args: str) -> str:
    """Run git with a deterministic identity and return stdout."""
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(worktree: str, name: str, content: str) -> None:
    with open(os.path.join(worktree, name), "w", encoding="utf-8") as handle:
        handle.write(content)


def _commit(worktree: str, message: str) -> str:
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    return W.head_commit(worktree)


def _count_merge_commits(worktree: str) -> int:
    """Merge commits reachable from HEAD."""
    output = _git(worktree, "rev-list", "--merges", "HEAD")
    return len([line for line in output.splitlines() if line.strip()])


@pytest.fixture
def divergable_branch(tmp_path):
    """Two working copies of ``env/dev`` sharing one remote.

    ``mine`` is the copy under test; ``theirs`` stands for a teammate, and is the
    only way to produce the remote-side commits a real pull has to integrate.
    """
    remote = str(tmp_path / "remote.git")
    _git(str(tmp_path), "init", "--bare", "-b", "main", remote)

    theirs = str(tmp_path / "theirs")
    _git(str(tmp_path), "clone", remote, theirs)
    _write(theirs, "model.sql", "SELECT 1 AS a\n")
    _commit(theirs, "baseline")
    _git(theirs, "push", "-u", "origin", "main")
    _git(theirs, "checkout", "-b", "env/dev")
    _git(theirs, "push", "-u", "origin", "env/dev")

    mine = str(tmp_path / "mine")
    _git(str(tmp_path), "clone", "--branch", "env/dev", remote, mine)
    return {"remote": remote, "mine": mine, "theirs": theirs}


def test_up_to_date_pull_writes_nothing(divergable_branch):
    """An unchanged branch is reported as such, so the context stays cached."""
    mine = divergable_branch["mine"]
    head_before = W.head_commit(mine)

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_UP_TO_DATE
    assert result.integrated_commits == 0
    assert W.head_commit(mine) == head_before


def test_local_only_commits_are_still_up_to_date(divergable_branch):
    """Being AHEAD of the remote is nothing for a pull to integrate."""
    mine = divergable_branch["mine"]
    _write(mine, "mine.sql", "SELECT 2 AS b\n")
    head = _commit(mine, "my work")

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_UP_TO_DATE
    assert W.head_commit(mine) == head


def test_behind_branch_fast_forwards_without_a_merge_commit(divergable_branch):
    """Only the remote advanced: history stays linear."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]
    _write(theirs, "theirs.sql", "SELECT 3 AS c\n")
    remote_head = _commit(theirs, "their work")
    _git(theirs, "push", "origin", "env/dev")

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_FAST_FORWARD
    assert result.commit == remote_head
    assert result.integrated_commits == 1
    assert _count_merge_commits(mine) == 0
    assert os.path.exists(os.path.join(mine, "theirs.sql"))


def test_divergent_branch_is_integrated_with_a_merge_commit(divergable_branch):
    """The case a plain ``git pull`` refuses: both sides advanced."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]

    _write(theirs, "theirs.sql", "SELECT 3 AS c\n")
    _commit(theirs, "their work")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "mine.sql", "SELECT 2 AS b\n")
    mine_head = _commit(mine, "my work")

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_MERGED
    assert result.integrated_commits == 1
    assert result.commit == W.head_commit(mine)
    # A real two-parent merge: both sides survived, neither was rewritten.
    assert len(_git(mine, "rev-list", "--parents", "-n1", result.commit).split()) == 3
    assert mine_head in _git(mine, "rev-list", "HEAD").splitlines()
    assert os.path.exists(os.path.join(mine, "theirs.sql"))
    assert os.path.exists(os.path.join(mine, "mine.sql"))


def test_divergent_pull_can_be_refused_when_merging_is_not_allowed(divergable_branch):
    """``allow_merge=False`` turns divergence into an explicit rejection."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]

    _write(theirs, "theirs.sql", "SELECT 3 AS c\n")
    _commit(theirs, "their work")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "mine.sql", "SELECT 2 AS b\n")
    head_before = _commit(mine, "my work")

    result = GitOperations.pull(mine, "env/dev", allow_merge=False)

    assert not result.ok
    assert result.reason == "non_fast_forward"
    assert W.head_commit(mine) == head_before


def test_conflicting_pull_succeeds_and_leaves_conflicts_in_the_working_tree(
    divergable_branch,
):
    """A conflict is the start of a resolution, not a failed operation."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]

    _write(theirs, "model.sql", "SELECT 99 AS a\n")
    _commit(theirs, "their edit")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "model.sql", "SELECT 42 AS a\n")
    _commit(mine, "my edit")

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_CONFLICTED
    assert result.conflicts == ["model.sql"]
    # The conflict is in the file the editor opens, which is the point.
    with open(os.path.join(mine, "model.sql"), encoding="utf-8") as handle:
        assert "<<<<<<<" in handle.read()


def test_dirty_working_copy_blocks_the_pull_before_anything_is_written(
    divergable_branch,
):
    """Uncommitted tracked work is never merged over."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]
    _write(theirs, "theirs.sql", "SELECT 3 AS c\n")
    _commit(theirs, "their work")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "model.sql", "SELECT 7 AS a -- work in progress\n")
    head_before = W.head_commit(mine)

    result = GitOperations.pull(mine, "env/dev")

    assert not result.ok
    assert result.outcome == PULL_BLOCKED
    assert W.head_commit(mine) == head_before
    assert not os.path.exists(os.path.join(mine, "theirs.sql"))


def test_untracked_files_do_not_block_the_pull(divergable_branch):
    """Untracked files cannot conflict with a merge, so they are ignored."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]
    _write(theirs, "theirs.sql", "SELECT 3 AS c\n")
    _commit(theirs, "their work")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "scratch.txt", "notes\n")

    result = GitOperations.pull(mine, "env/dev")

    assert result.ok
    assert result.outcome == PULL_FAST_FORWARD
    assert os.path.exists(os.path.join(mine, "scratch.txt"))


def test_unresolved_earlier_merge_is_reported_as_such(divergable_branch):
    """A half-finished merge is named, not mislabelled as "uncommitted work"."""
    mine, theirs = divergable_branch["mine"], divergable_branch["theirs"]

    _write(theirs, "model.sql", "SELECT 99 AS a\n")
    _commit(theirs, "their edit")
    _git(theirs, "push", "origin", "env/dev")

    _write(mine, "model.sql", "SELECT 42 AS a\n")
    _commit(mine, "my edit")

    first = GitOperations.pull(mine, "env/dev")
    assert first.outcome == PULL_CONFLICTED

    second = GitOperations.pull(mine, "env/dev")

    assert not second.ok
    assert second.outcome == PULL_BLOCKED
    assert second.conflicts == ["model.sql"]
    assert "unresolved" in second.detail


def test_unknown_remote_branch_is_reported_not_guessed(divergable_branch):
    """A branch absent on the remote yields a branch-not-found reason."""
    result = GitOperations.pull(divergable_branch["mine"], "env/nope")

    assert not result.ok
    assert result.reason == "branch_not_found"


def test_branch_name_that_could_be_an_option_is_rejected(divergable_branch):
    """Refs are validated before they can reach the git command line."""
    result = GitOperations.pull(divergable_branch["mine"], "--upload-pack=evil")

    assert not result.ok
    assert result.reason == "branch_not_found"
