"""Materialization tests (design 06 P4).

Run against REAL git, using a local bare repository as the "remote". A mocked
git would only assert that this module calls the functions it calls; what needs
pinning is git's actual behaviour — that a worktree of a fetched branch tracks
it, that `worktree add` refuses a stale administrative record, that a bare
clone leaves no HEAD when interrupted. Those are the facts the idempotence
contract rests on.

The interrupted-attempt cases matter most: treating "directory exists" as
"materialized" would wedge a workspace permanently, since nothing would ever
retry the clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lease.materialize import (  # noqa: E402
    MaterializationError,
    discard,
    is_valid_bare_repo,
    is_valid_worktree,
    materialize,
)

_KEY = "t1/ws-t1-shop/u1/dev"
_PROD_KEY = "t1/ws-t1-shop/u1/prod"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for materialization tests"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def remote(tmp_path: Path) -> str:
    """A bare repository standing in for the Git server."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "model.sql").write_text("SELECT 1;\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    # A branch for the `dev` environment, matching branch_of("dev") == "env/dev".
    _git(work, "branch", "env/dev")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(work), str(bare))
    return str(bare)


@pytest.fixture()
def root(tmp_path: Path) -> str:
    materialize_root = tmp_path / "workcopies"
    materialize_root.mkdir()
    return str(materialize_root)


class TestFirstMaterialization:
    def test_creates_shared_repo_and_worktree(self, root, remote):
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)

        assert copy.freshly_materialized is True
        assert copy.branch == "env/dev"
        assert is_valid_bare_repo(copy.repo_path)
        assert is_valid_worktree(copy.workspace_path)
        # The working copy must actually contain the repository's content.
        assert (Path(copy.workspace_path) / "model.sql").read_text() == "SELECT 1;\n"

    def test_layout_matches_the_platform_convention(self, root, remote):
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        expected = Path(root) / "t1" / "ws-t1-shop" / "u1"
        assert Path(copy.user_root) == expected
        assert Path(copy.repo_path) == expected / ".repo"
        assert Path(copy.workspace_path) == expected / "envs" / "dev"

    def test_worktree_tracks_the_remote_branch(self, root, remote):
        # Without an upstream, later pull/push would need explicit arguments and
        # the environment/branch 1:1 invariant would be unenforceable.
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=copy.workspace_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "origin/env/dev"

    def test_environment_without_a_remote_branch_starts_from_head(self, root, remote):
        # A brand-new environment has no branch yet; that is normal, not an error.
        copy = materialize(materialize_root=root, lease_key=_PROD_KEY, repo_url=remote)
        assert is_valid_worktree(copy.workspace_path)
        assert (Path(copy.workspace_path) / "model.sql").exists()

    def test_second_environment_reuses_the_object_database(self, root, remote):
        # Disk must be O(1) in the number of environments: this is why `.repo`
        # is shared rather than cloned per environment.
        first = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        second = materialize(
            materialize_root=root, lease_key=_PROD_KEY, repo_url=remote
        )
        assert first.repo_path == second.repo_path
        assert first.workspace_path != second.workspace_path


class TestIdempotence:
    def test_repeat_call_is_a_cache_hit(self, root, remote):
        # Every request for a leased workspace calls materialize(), so the warm
        # path must be free of side effects.
        materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        again = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        assert again.freshly_materialized is False

    def test_uncommitted_changes_survive_a_cache_hit(self, root, remote):
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        dirty = Path(copy.workspace_path) / "model.sql"
        dirty.write_text("SELECT 2; -- work in progress\n")

        materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        assert "work in progress" in dirty.read_text()

    def test_cache_hit_needs_no_repo_url(self, root, remote):
        # After binding, the remote may legitimately be unknown to the caller.
        materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        assert (
            materialize(materialize_root=root, lease_key=_KEY).freshly_materialized
            is False
        )


class TestRecoveryFromInterruptedAttempts:
    def test_partial_bare_repo_is_rebuilt(self, root, remote):
        """A clone killed mid-flight leaves a directory with no HEAD.

        If presence were taken for validity, this workspace could never be
        materialized again — no code path would retry the clone.
        """
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        os.unlink(os.path.join(copy.repo_path, "HEAD"))
        assert is_valid_bare_repo(copy.repo_path) is False

        recovered = materialize(
            materialize_root=root, lease_key=_KEY, repo_url=remote
        )
        assert recovered.freshly_materialized is True
        assert is_valid_bare_repo(recovered.repo_path)

    def test_stray_directory_at_the_worktree_path_is_replaced(self, root, remote):
        # `mkdir` succeeded, `worktree add` never ran.
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        shutil.rmtree(copy.workspace_path)
        Path(copy.workspace_path).mkdir(parents=True)
        (Path(copy.workspace_path) / "junk").write_text("x")
        assert is_valid_worktree(copy.workspace_path) is False

        recovered = materialize(materialize_root=root, lease_key=_KEY)
        assert recovered.freshly_materialized is True
        assert is_valid_worktree(recovered.workspace_path)

    def test_worktree_lost_with_a_stale_git_record_is_re_added(self, root, remote):
        """The state an `ephemeral` medium produces on every pod restart.

        The directory is gone but `.repo` still records the worktree, and git
        refuses to re-add one it believes is registered — hence the prune.
        """
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        shutil.rmtree(copy.workspace_path)

        recovered = materialize(materialize_root=root, lease_key=_KEY)
        assert is_valid_worktree(recovered.workspace_path)
        assert (Path(recovered.workspace_path) / "model.sql").exists()


class TestFailures:
    def test_missing_objects_and_no_url_is_an_error(self, root):
        with pytest.raises(MaterializationError, match="no object database"):
            materialize(materialize_root=root, lease_key=_KEY)

    def test_unreachable_remote_leaves_nothing_behind(self, root, tmp_path):
        # A partial `.repo` must not accumulate, nor be mistaken for valid later.
        missing = str(tmp_path / "does-not-exist.git")
        with pytest.raises(MaterializationError, match="cannot clone"):
            materialize(materialize_root=root, lease_key=_KEY, repo_url=missing)
        assert not os.path.exists(
            os.path.join(root, "t1", "ws-t1-shop", "u1", ".repo")
        )

    def test_malformed_lease_key_is_rejected(self, root):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            materialize(materialize_root=root, lease_key="garbage")


class TestDiscard:
    def test_discard_removes_the_worktree_but_keeps_the_objects(self, root, remote):
        # LRU reclamation: `.repo` may serve this user's other environments and
        # is expensive to re-fetch.
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        discard(root, _KEY)

        assert not os.path.exists(copy.workspace_path)
        assert is_valid_bare_repo(copy.repo_path)

    def test_discarded_copy_can_be_re_materialized(self, root, remote):
        materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        discard(root, _KEY)
        again = materialize(materialize_root=root, lease_key=_KEY)
        assert is_valid_worktree(again.workspace_path)

    def test_discard_is_idempotent(self, root, remote):
        materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        discard(root, _KEY)
        discard(root, _KEY)


class TestBareCloneRefLayout:
    """`git clone --bare` is not a mirror, and the difference is load-bearing.

    It writes the remote's branches straight into `refs/heads/*`, creates no
    `refs/remotes/*` and installs no fetch refspec. Left as-is: every remote
    branch looks local (so `worktree add` configures no upstream), and a later
    fetch would overwrite branches a worktree has checked out.
    """

    def _refs(self, repo: str, namespace: str) -> list[str]:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", namespace],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def test_remote_state_is_namespaced_under_refs_remotes(self, root, remote):
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        remotes = self._refs(copy.repo_path, "refs/remotes")
        assert "refs/remotes/origin/env/dev" in remotes
        assert "refs/remotes/origin/main" in remotes

    def test_only_materialized_branches_remain_local(self, root, remote):
        # `env/dev` is local because we materialized it; the default branch
        # survives because HEAD points at it and git refuses to delete it.
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        heads = self._refs(copy.repo_path, "refs/heads")
        assert "refs/heads/env/dev" in heads
        assert set(heads) <= {"refs/heads/env/dev", "refs/heads/main"}

    def test_fetch_refspec_is_installed(self, root, remote):
        # Without it, `git fetch` would land remote updates directly on
        # refs/heads/* and desynchronize a user's checked-out work.
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.fetch"],
            cwd=copy.repo_path,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "+refs/heads/*:refs/remotes/origin/*"

    def test_fetch_does_not_move_a_checked_out_branch(self, root, remote):
        """The concrete data-loss scenario the refspec prevents."""
        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)
        before = subprocess.run(
            ["git", "rev-parse", "refs/heads/env/dev"],
            cwd=copy.repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # A colleague pushes to the same branch on the server. `seed` has no
        # `origin` (the bare repo was cloned FROM it), so push by path.
        seed = Path(remote).parent / "seed"
        _git(seed, "checkout", "env/dev")
        (seed / "model.sql").write_text("SELECT 99;\n")
        _git(seed, "commit", "-am", "colleague")
        _git(seed, "push", str(remote), "env/dev")

        materialize(materialize_root=root, lease_key=_KEY, fetch_if_present=True)

        after = subprocess.run(
            ["git", "rev-parse", "refs/heads/env/dev"],
            cwd=copy.repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # The LOCAL branch must not move under the user's feet; only the
        # remote-tracking ref advances.
        assert after == before
        assert (
            subprocess.run(
                ["git", "rev-parse", "refs/remotes/origin/env/dev"],
                cwd=copy.repo_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            != before
        )


class TestHooksAreNeverExecuted:
    def test_post_checkout_hook_from_the_repository_does_not_run(self, root, remote):
        """Hooks are USER CONTENT and must never execute as this service.

        Materialization is exactly where `post-checkout` would fire, so this is
        the regression test for the platform-wide `core.hooksPath=/dev/null`.
        """
        hooks = Path(remote) / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = Path(root) / "hook-executed"
        hook = hooks / "post-checkout"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o755)

        copy = materialize(materialize_root=root, lease_key=_KEY, repo_url=remote)

        assert is_valid_worktree(copy.workspace_path)
        assert not marker.exists(), "a repository hook executed inside the platform"
