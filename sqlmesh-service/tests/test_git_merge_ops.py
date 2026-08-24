"""Tests for the two branch-merge primitives (real git, real remote).

They cover the contract the platform's split promotion relies on:

* the PROMOTIONAL merge publishes on the remote without touching any working
  copy, so a member holding the target branch checked out is never mutated
  behind their back;
* a conflict is DETECTED before anything is written (``merge-tree`` pre-flight);
* the SYNC merge leaves the conflict in the caller's working tree, which is the
  only place it can be resolved.

The fixture reproduces the platform layout: one bare repository shared by the
worktrees of one user, plus a separate "remote" the promotion is pushed to.
"""

import os
import shutil
import subprocess

import pytest

from src.workspace.git_ops import GitMergeOperations as M
from src.workspace.git_ops import GitWorktreeOperations as W

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable is required"
)


def _git(cwd: str, *args: str) -> str:
    """Run git with a deterministic identity and return stdout."""
    result = subprocess.run(
        [
            "git",
            "-c", "user.name=Test",
            "-c", "user.email=test@localhost",
            *args,
        ],
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


@pytest.fixture
def two_environments(tmp_path):
    """Layout: a remote, a shared bare repo, and two environment worktrees.

    ``main`` (production) and ``env/dev`` (a sandbox) both exist locally AND on
    the remote, which is the state every merge reasons about.
    """
    remote = str(tmp_path / "remote.git")
    _git(str(tmp_path), "init", "--bare", "-b", "main", remote)

    repo = str(tmp_path / "u1" / ".repo")
    assert W.init_bare(repo, "main")
    _git(repo, "remote", "add", "origin", remote)

    prod = str(tmp_path / "u1" / "envs" / "prod")
    assert W.create_empty_root_commit(repo, "main")
    assert W.worktree_add(repo, prod, "main").ok
    _write(prod, "model.sql", "SELECT 1 AS a\n")
    _commit(prod, "prod baseline")
    _git(prod, "push", "-u", "origin", "main")

    dev = str(tmp_path / "u1" / "envs" / "dev")
    assert W.worktree_add(repo, dev, "env/dev", start_point="main").ok
    return {"remote": remote, "repo": repo, "prod": prod, "dev": dev}


def test_clean_promotion_publishes_without_touching_any_worktree(two_environments):
    """The merge lands on the remote; local working copies stay exactly as they were."""
    dev, prod = two_environments["dev"], two_environments["prod"]
    _write(dev, "new_model.sql", "SELECT 2 AS b\n")
    _commit(dev, "add new model")
    _git(dev, "push", "-u", "origin", "env/dev")
    _git(dev, "fetch", "origin")

    prod_head_before = W.head_commit(prod)
    dev_head_before = W.head_commit(dev)

    target_ref = M.resolve_ref(dev, "main")
    source_ref = M.resolve_ref(dev, "env/dev")
    probe = M.probe_merge(dev, target_ref, source_ref)
    assert probe.clean, probe.conflicts

    merge_commit = M.merge_in_bare(
        dev, target_ref, source_ref, "Merge branch 'env/dev' into 'main'", probe.tree
    )
    assert merge_commit
    assert M.push_commit(dev, merge_commit, "main").ok

    # The remote advanced...
    assert _git(dev, "ls-remote", "origin", "refs/heads/main").split()[0] == merge_commit
    # ...while no working copy moved, and the merge commit has two parents so the
    # promotion stays identifiable and revertible as one unit.
    assert W.head_commit(prod) == prod_head_before
    assert W.head_commit(dev) == dev_head_before
    assert len(_git(dev, "rev-list", "--parents", "-n1", merge_commit).split()) == 3


def test_promotion_conflict_is_detected_before_anything_is_written(two_environments):
    """A conflicting merge is refused by the pre-flight, leaving no trace."""
    dev, prod = two_environments["dev"], two_environments["prod"]

    _write(prod, "model.sql", "SELECT 99 AS a\n")
    _commit(prod, "prod hotfix")
    _git(prod, "push", "origin", "main")

    _write(dev, "model.sql", "SELECT 42 AS a\n")
    _commit(dev, "dev change")
    _git(dev, "push", "-u", "origin", "env/dev")
    _git(dev, "fetch", "origin")

    remote_main_before = _git(dev, "ls-remote", "origin", "refs/heads/main").split()[0]

    probe = M.probe_merge(dev, M.resolve_ref(dev, "main"), M.resolve_ref(dev, "env/dev"))
    assert probe.ok
    assert probe.conflicts == ["model.sql"]
    assert not probe.clean

    # The refusal is the whole point: the remote is untouched.
    assert (
        _git(dev, "ls-remote", "origin", "refs/heads/main").split()[0]
        == remote_main_before
    )


def test_sync_leaves_the_conflict_in_the_callers_working_tree(two_environments):
    """The resolution path: the conflict lands where an editor can fix it."""
    dev, prod = two_environments["dev"], two_environments["prod"]

    _write(prod, "model.sql", "SELECT 99 AS a\n")
    _commit(prod, "prod hotfix")
    _git(prod, "push", "origin", "main")

    _write(dev, "model.sql", "SELECT 42 AS a\n")
    _commit(dev, "dev change")
    _git(dev, "fetch", "origin")

    result = M.merge_in_worktree(
        dev, M.resolve_ref(dev, "main"), "Merge branch 'main' into 'env/dev'"
    )
    # A conflicted sync is a successful START of a resolution, not a failure.
    assert result.ok
    assert result.conflicts == ["model.sql"]
    assert M.unmerged_paths(dev) == ["model.sql"]
    with open(os.path.join(dev, "model.sql"), encoding="utf-8") as handle:
        assert "<<<<<<<" in handle.read()

    # And it is abandonable, so a user is never stuck in a half merge.
    assert M.abort_merge(dev)
    assert M.unmerged_paths(dev) == []


def test_clean_sync_fast_forwards_only_the_callers_branch(two_environments):
    dev, prod = two_environments["dev"], two_environments["prod"]

    _write(prod, "prod_only.sql", "SELECT 3 AS c\n")
    _commit(prod, "prod addition")
    _git(prod, "push", "origin", "main")
    _git(dev, "fetch", "origin")

    prod_head_before = W.head_commit(prod)
    result = M.merge_in_worktree(
        dev, M.resolve_ref(dev, "main"), "Merge branch 'main' into 'env/dev'"
    )

    assert result.ok and not result.conflicts
    assert os.path.exists(os.path.join(dev, "prod_only.sql"))
    # The other environment is untouched: worktrees share objects, not state.
    assert W.head_commit(prod) == prod_head_before


def test_branch_diff_reports_changes_against_the_merge_base(two_environments):
    """Three-dot semantics: only what the SOURCE changed, never the target's own commits."""
    dev, prod = two_environments["dev"], two_environments["prod"]

    _write(prod, "prod_only.sql", "SELECT 3 AS c\n")
    _commit(prod, "prod addition")
    _git(prod, "push", "origin", "main")

    _write(dev, "dev_only.sql", "SELECT 4 AS d\n")
    _commit(dev, "dev addition")
    _git(dev, "fetch", "origin")

    files = M.diff_files(dev, M.resolve_ref(dev, "main"), M.resolve_ref(dev, "env/dev"))
    # ``prod_only.sql`` exists only on the target, so a two-dot diff would report
    # it as deleted by the source — the merge would apply no such deletion.
    assert [(entry.status, entry.path) for entry in files] == [("A", "dev_only.sql")]

    commits = M.list_commits(
        dev, M.resolve_ref(dev, "main"), M.resolve_ref(dev, "env/dev")
    )
    assert [commit["message"] for commit in commits] == ["dev addition"]


def test_resolve_ref_prefers_the_shared_remote_state(two_environments, tmp_path):
    """A promotion must never publish a stale local copy of a shared branch.

    The realistic staleness is another MEMBER advancing the branch: within one
    user's repository the local ref moves with their own worktree.
    """
    dev, remote = two_environments["dev"], two_environments["remote"]

    other = str(tmp_path / "member2")
    _git(str(tmp_path), "clone", remote, other)
    _write(other, "model.sql", "SELECT 7 AS a\n")
    _commit(other, "another member moved prod on")
    _git(other, "push", "origin", "main")

    _git(dev, "fetch", "origin")

    assert M.resolve_ref(dev, "main") == "refs/remotes/origin/main"
    # The local ref still exists but is behind, which is exactly why it loses:
    # merging it would promote code the team has already superseded.
    assert M.count_commits(dev, "refs/heads/main", "refs/remotes/origin/main") == 1
    assert M.resolve_ref(dev, "unknown_branch") == ""
