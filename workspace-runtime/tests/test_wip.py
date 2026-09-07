"""WIP snapshot tests (design 07 Q6).

These assert a PUBLISHED durability contract, so they are written as statements
about user-visible outcomes rather than about calls made:

* a snapshot must not disturb the working copy (the mechanism runs while the
  user types);
* uncommitted work must survive the total loss of the node's disk;
* a graceful drain must lose nothing at all;
* a failed snapshot must be visible, because a user who believes they are
  protected and is not is worse off than one who knows they are not.

Run against real git: the guarantees rest on `stash create` semantics and on
hidden refs actually reaching the server, neither of which a mock can attest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lease.materialize import materialize  # noqa: E402
from src.lease.wip import (  # noqa: E402
    SnapshotScheduler,
    apply_snapshot,
    discard_snapshot,
    find_snapshot,
    take_snapshot,
    wip_ref,
    wip_ref_for_lease,
)

_KEY = "t1/ws-t1-shop/u1/dev"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for WIP snapshot tests"
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def remote(tmp_path: Path) -> str:
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "model.sql").write_text("SELECT 1;\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    _git(work, "branch", "env/dev")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(work), str(bare))
    return str(bare)


@pytest.fixture()
def copy(tmp_path: Path, remote: str):
    """A materialized working copy with git identity configured."""
    root = tmp_path / "workcopies"
    root.mkdir()
    materialized = materialize(
        materialize_root=str(root), lease_key=_KEY, repo_url=remote
    )
    # `stash create` writes a commit, which needs an author.
    _git(materialized.workspace_path, "config", "user.email", "u1@example.com")
    _git(materialized.workspace_path, "config", "user.name", "U1")
    return materialized


def _remote_ref_exists(remote: str, ref: str) -> bool:
    result = _git(remote, "rev-parse", "--verify", "--quiet", ref, check=False)
    return result.returncode == 0


class TestRefNaming:
    def test_snapshots_are_hidden_refs_not_branches(self):
        # Under refs/heads/* they would show up in every branch picker and be
        # fetched by a plain clone, and could be mistaken for the user's work.
        ref = wip_ref("u1", "dev")
        assert ref == "refs/datacode-wip/u1/dev"
        assert not ref.startswith("refs/heads/")

    def test_ref_is_derived_from_the_lease_key(self):
        assert wip_ref_for_lease(_KEY) == "refs/datacode-wip/u1/dev"

    def test_malformed_lease_key_is_rejected(self):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            wip_ref_for_lease("garbage")


class TestSnapshotDoesNotDisturbTheUser:
    def test_working_tree_is_untouched(self, copy):
        """The mechanism runs while the user is editing.

        A periodic `git stash push` would repeatedly revert their file, which is
        worse than the loss the snapshot prevents.
        """
        target = Path(copy.workspace_path) / "model.sql"
        target.write_text("SELECT 42; -- mid edit\n")

        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )

        assert target.read_text() == "SELECT 42; -- mid edit\n"

    def test_index_and_status_are_unchanged(self, copy):
        target = Path(copy.workspace_path) / "model.sql"
        target.write_text("SELECT 42;\n")
        before = _git(copy.workspace_path, "status", "--porcelain").stdout

        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )

        assert _git(copy.workspace_path, "status", "--porcelain").stdout == before

    def test_no_stash_entry_is_created(self, copy):
        # `create` must be used rather than `push`: a growing stash list would
        # be visible to the user and confusing.
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 42;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        assert _git(copy.workspace_path, "stash", "list").stdout.strip() == ""


class TestCleanWorkingCopyIsFree:
    def test_clean_copy_reports_protected_without_pushing(self, copy, remote):
        # Thousands of idle leases must not each cost a network round trip.
        result = take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        assert result.was_clean is True
        assert result.protected is True
        assert not _remote_ref_exists(remote, wip_ref_for_lease(_KEY))


class TestDurabilityContract:
    def test_uncommitted_work_survives_total_loss_of_the_disk(
        self, copy, remote, tmp_path
    ):
        """The contract's core claim, exercised end to end.

        Write -> snapshot -> destroy the entire local tree (the `kill -9` +
        `ephemeral` medium case) -> re-materialize elsewhere -> recover.
        """
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 777;\n")
        (Path(copy.workspace_path) / "new_model.sql").write_text("SELECT 888;\n")
        _git(copy.workspace_path, "add", "new_model.sql")

        result = take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        assert result.protected is True
        assert _remote_ref_exists(remote, wip_ref_for_lease(_KEY))

        # The node is gone: nothing local survives.
        shutil.rmtree(Path(copy.user_root).parent.parent)

        fresh_root = tmp_path / "replacement"
        fresh_root.mkdir()
        recovered_copy = materialize(
            materialize_root=str(fresh_root), lease_key=_KEY, repo_url=remote
        )
        _git(recovered_copy.workspace_path, "config", "user.email", "u1@example.com")
        _git(recovered_copy.workspace_path, "config", "user.name", "U1")

        snapshot = find_snapshot(lease_key=_KEY, repo_path=recovered_copy.repo_path)
        assert snapshot is not None
        assert apply_snapshot(
            workspace_path=recovered_copy.workspace_path, snapshot=snapshot
        )

        assert (
            Path(recovered_copy.workspace_path) / "model.sql"
        ).read_text() == "SELECT 777;\n"
        assert (
            Path(recovered_copy.workspace_path) / "new_model.sql"
        ).read_text() == "SELECT 888;\n"

    def test_committed_but_unpushed_work_is_also_protected(self, copy, remote):
        # The contract promises zero loss for this row, because the commit is
        # reachable from the snapshot's parent.
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 555;\n")
        _git(copy.workspace_path, "commit", "-am", "local work")
        head = _git(copy.workspace_path, "rev-parse", "HEAD").stdout.strip()

        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 556;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )

        # The snapshot's first parent is the local commit, so pushing the
        # snapshot carried it to the server too.
        parent = _git(
            remote, "rev-parse", f"{wip_ref_for_lease(_KEY)}^1"
        ).stdout.strip()
        assert parent == head

    def test_later_snapshot_supersedes_the_earlier_one(self, copy, remote):
        # Snapshots are not descendants of each other, so the push must be
        # forced or every update after the first would be rejected.
        target = Path(copy.workspace_path) / "model.sql"
        target.write_text("SELECT 1;-- first\n")
        first = take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        target.write_text("SELECT 2;-- second\n")
        second = take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )

        assert second.protected is True
        assert second.commit != first.commit
        tip = _git(remote, "rev-parse", wip_ref_for_lease(_KEY)).stdout.strip()
        assert tip == second.commit


class TestRecovery:
    def test_absent_snapshot_is_not_an_error(self, copy):
        # The normal case: no unsaved work.
        assert find_snapshot(lease_key=_KEY, repo_path=copy.repo_path) is None

    def test_apply_refuses_a_dirty_working_copy(self, copy):
        """No safe automatic answer exists here.

        Merging a snapshot into unrelated uncommitted changes could destroy
        either side, so the caller must resolve it.
        """
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 1;-- snap\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        snapshot = find_snapshot(lease_key=_KEY, repo_path=copy.repo_path)
        assert snapshot is not None

        # Still dirty from the edit above.
        assert (
            apply_snapshot(workspace_path=copy.workspace_path, snapshot=snapshot)
            is False
        )

    def test_discard_removes_the_server_ref(self, copy, remote):
        # Otherwise the recovery prompt reappears forever and users learn to
        # dismiss it -- defeating the mechanism the next time it matters.
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 1;-- x\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=copy.workspace_path,
            repo_path=copy.repo_path,
        )
        assert _remote_ref_exists(remote, wip_ref_for_lease(_KEY))

        assert discard_snapshot(lease_key=_KEY, repo_path=copy.repo_path) is True
        assert not _remote_ref_exists(remote, wip_ref_for_lease(_KEY))


class TestScheduler:
    def _scheduler(self, copy, **overrides) -> SnapshotScheduler:
        kwargs = {
            "interval_seconds": 0.1,
            "lease_provider": lambda: [
                (_KEY, copy.workspace_path, copy.repo_path)
            ],
        }
        kwargs.update(overrides)
        return SnapshotScheduler(**kwargs)

    async def test_periodic_loop_snapshots_dirty_copies(self, copy, remote):
        import asyncio

        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 9;\n")
        scheduler = self._scheduler(copy)
        await scheduler.start()
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if _remote_ref_exists(remote, wip_ref_for_lease(_KEY)):
                    break
            assert _remote_ref_exists(remote, wip_ref_for_lease(_KEY))
        finally:
            await scheduler.stop(final_snapshot=False)

    async def test_drain_takes_a_final_snapshot(self, copy, remote):
        """This is what makes a graceful drain ZERO loss.

        Without it the guarantee would degrade to "up to one interval", i.e. the
        same as a crash.
        """
        scheduler = self._scheduler(copy, interval_seconds=3600)
        await scheduler.start()
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 10;\n")

        await scheduler.stop()  # final_snapshot defaults to True

        assert _remote_ref_exists(remote, wip_ref_for_lease(_KEY))

    async def test_last_protected_at_is_exposed(self, copy):
        # Feeds the frontend's "last protected at" indicator, which is what
        # makes the risk window visible to the user.
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 11;\n")
        scheduler = self._scheduler(copy)
        assert scheduler.last_protected_at(_KEY) is None

        await scheduler.snapshot_all()

        stamp = scheduler.last_protected_at(_KEY)
        assert stamp is not None
        assert stamp.tzinfo is not None, "timestamps must be tz-aware UTC"
        assert scheduler.is_protected(_KEY) is True

    async def test_clean_copy_does_not_advance_last_protected_at(self, copy):
        # A clean copy has nothing at risk; reporting a fresh protection time
        # would overstate what happened.
        scheduler = self._scheduler(copy)
        await scheduler.snapshot_all()
        assert scheduler.last_protected_at(_KEY) is None
        assert scheduler.is_protected(_KEY) is True

    async def test_failed_snapshot_marks_the_lease_unprotected(self, copy, remote):
        """A silent failure is the worst outcome: the user believes otherwise."""
        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 12;\n")
        # Server unreachable.
        shutil.rmtree(remote)

        scheduler = self._scheduler(copy)
        results = await scheduler.snapshot_all()

        assert results and results[0].protected is False
        assert scheduler.is_protected(_KEY) is False
        assert scheduler.unprotected_leases() == (_KEY,)

    async def test_recovery_clears_the_unprotected_flag(self, copy, remote):
        import asyncio

        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 13;\n")
        moved = Path(remote).with_name("moved.git")
        shutil.move(remote, moved)

        scheduler = self._scheduler(copy)
        await scheduler.snapshot_all()
        assert scheduler.is_protected(_KEY) is False

        shutil.move(moved, remote)
        await asyncio.sleep(0)
        await scheduler.snapshot_all()
        assert scheduler.is_protected(_KEY) is True

    async def test_one_failing_copy_does_not_stop_the_others(self, copy):
        def provider():
            # A bogus entry first: its failure must not prevent the real one.
            yield ("t1/ws-t1-shop/u9/dev", "/nonexistent/path", "/nonexistent/repo")
            yield (_KEY, copy.workspace_path, copy.repo_path)

        (Path(copy.workspace_path) / "model.sql").write_text("SELECT 14;\n")
        scheduler = self._scheduler(copy, lease_provider=provider)
        await scheduler.snapshot_all()
        assert scheduler.is_protected(_KEY) is True
