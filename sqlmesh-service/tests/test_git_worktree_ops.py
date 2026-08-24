"""Tests for the bare-repository + worktree primitives (real git).

These cover the Phase 1 acceptance criterion: one user can hold SEVERAL
environment working copies at the same time, and what Git reports must match
what the platform believes.
"""

import os
import shutil
import subprocess

import pytest

from src.workspace.git_ops import GitWorktreeOperations as W

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable is required"
)


@pytest.fixture
def bootstrapped_repo(tmp_path):
    """A bare repository whose ``main`` branch carries one empty root commit."""
    repo = str(tmp_path / "u1" / ".repo")
    assert W.init_bare(repo, "main")
    commit = W.create_empty_root_commit(repo, "main")
    assert commit
    return repo, commit


def _write_and_commit(worktree: str, filename: str) -> None:
    """Create a file and commit it inside a worktree."""
    with open(os.path.join(worktree, filename), "w", encoding="utf-8") as handle:
        handle.write("content\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            f"add {filename}",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )


def test_init_bare_is_idempotent(tmp_path):
    repo = str(tmp_path / ".repo")
    assert W.init_bare(repo, "main")
    assert W.init_bare(repo, "main")
    assert os.path.exists(os.path.join(repo, "HEAD"))


def test_bootstrap_creates_a_branch_worktrees_can_attach_to(bootstrapped_repo, tmp_path):
    repo, commit = bootstrapped_repo
    assert W.local_branch_exists(repo, "main")

    target = str(tmp_path / "u1" / "envs" / "prod")
    assert W.worktree_add(repo, target, "main").ok
    assert W.head_commit(target) == commit


def test_three_environments_coexist_and_match_git(bootstrapped_repo, tmp_path):
    repo, _ = bootstrapped_repo
    envs = {"prod": "main", "dev_alice": "env/dev_alice", "feature_orders": "env/feature_orders"}

    for environment, branch in envs.items():
        target = str(tmp_path / "u1" / "envs" / environment)
        result = W.worktree_add(repo, target, branch, start_point="main")
        assert result.ok, result.detail

    entries = {
        os.path.basename(entry.path): entry.branch
        for entry in W.worktree_list(repo)
        if not entry.bare
    }
    assert entries == envs

    # Working copies are independent: a commit in one must not touch another.
    alice = str(tmp_path / "u1" / "envs" / "dev_alice")
    _write_and_commit(alice, "alice.sql")
    assert os.path.exists(os.path.join(alice, "alice.sql"))
    assert not os.path.exists(
        os.path.join(str(tmp_path / "u1" / "envs" / "feature_orders"), "alice.sql")
    )


def test_a_branch_cannot_be_checked_out_twice(bootstrapped_repo, tmp_path):
    """Git itself enforces the Environment <-> Branch 1:1 invariant."""
    repo, _ = bootstrapped_repo
    first = str(tmp_path / "u1" / "envs" / "prod")
    second = str(tmp_path / "u1" / "envs" / "prod_copy")

    assert W.worktree_add(repo, first, "main").ok
    assert not W.worktree_add(repo, second, "main").ok


def test_worktree_remove_keeps_the_branch(bootstrapped_repo, tmp_path):
    repo, _ = bootstrapped_repo
    target = str(tmp_path / "u1" / "envs" / "dev_alice")
    assert W.worktree_add(repo, target, "env/dev_alice", start_point="main").ok

    assert W.worktree_remove(repo, target).ok
    assert not os.path.isdir(target)
    # The environment's durable state is its branch, not the checkout.
    assert W.local_branch_exists(repo, "env/dev_alice")
    assert [entry for entry in W.worktree_list(repo) if not entry.bare] == []
