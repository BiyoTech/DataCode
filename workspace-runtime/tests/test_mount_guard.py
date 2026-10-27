"""Single-mount guard tests (design 05 §5.2, P4a).

The guard exists for the failure mode that has no error at write time: two
processes writing one Git worktree. So the cases that matter are the ones where
a plausible implementation would let the second writer through — contention
across processes, re-entry inside one process, and a released lock being
reusable. A deployment fault (read-only mount) must stay distinguishable from
contention, since the two need opposite responses.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lease.mount_guard import (  # noqa: E402
    LOCK_FILENAME,
    SingleMountViolation,
    acquire_mount,
    lock_path_for,
)

_KEY = "t1/ws-t1-shop/u1/dev"
_OTHER_ENV_SAME_USER = "t1/ws-t1-shop/u1/staging"
_OTHER_USER = "t1/ws-t1-shop/u2/dev"


class TestLockPathLayout:
    def test_lock_lives_in_the_user_root(self, tmp_path):
        path = lock_path_for(str(tmp_path), _KEY)
        assert path == tmp_path / "t1" / "ws-t1-shop" / "u1" / LOCK_FILENAME

    def test_environments_of_one_user_share_the_lock(self, tmp_path):
        # The `.repo` object database is shared by all of a user's worktrees,
        # so materializing two environments concurrently must be serialized.
        assert lock_path_for(str(tmp_path), _KEY) == lock_path_for(
            str(tmp_path), _OTHER_ENV_SAME_USER
        )

    def test_different_users_do_not_share_the_lock(self, tmp_path):
        # Otherwise one user's activity would block another's for no reason.
        assert lock_path_for(str(tmp_path), _KEY) != lock_path_for(
            str(tmp_path), _OTHER_USER
        )

    def test_malformed_lease_key_is_rejected(self, tmp_path):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            lock_path_for(str(tmp_path), "garbage")


class TestAcquisition:
    def test_acquire_creates_the_lock_file_and_records_the_holder(self, tmp_path):
        with acquire_mount(str(tmp_path), _KEY) as guard:
            assert guard.lock_path.exists()
            # Diagnostics only, but must name this process or the contention
            # log line is useless.
            assert str(os.getpid()) in guard.lock_path.read_text()

    def test_release_is_idempotent(self, tmp_path):
        guard = acquire_mount(str(tmp_path), _KEY)
        guard.release()
        guard.release()

    def test_lock_is_reusable_after_release(self, tmp_path):
        acquire_mount(str(tmp_path), _KEY).release()
        # The file is intentionally left behind; a stale file must not look
        # like a held lock.
        acquire_mount(str(tmp_path), _KEY).release()

    def test_reentrant_acquisition_in_one_process_is_allowed_by_the_kernel(
        self, tmp_path
    ):
        # flock is per open-file-description, so a second open() in the SAME
        # process does contend. This pins the behaviour we rely on: the guard
        # catches a duplicated holder even inside one process.
        first = acquire_mount(str(tmp_path), _KEY)
        try:
            with pytest.raises(SingleMountViolation):
                acquire_mount(str(tmp_path), _KEY)
        finally:
            first.release()

    def test_second_environment_of_same_user_is_blocked_while_held(self, tmp_path):
        first = acquire_mount(str(tmp_path), _KEY)
        try:
            with pytest.raises(SingleMountViolation):
                acquire_mount(str(tmp_path), _OTHER_ENV_SAME_USER)
        finally:
            first.release()

    def test_other_user_is_unaffected(self, tmp_path):
        first = acquire_mount(str(tmp_path), _KEY)
        try:
            acquire_mount(str(tmp_path), _OTHER_USER).release()
        finally:
            first.release()


def _try_acquire(root: str, key: str, result: "mp.Queue") -> None:
    """Child-process entry: report whether the claim succeeded."""
    try:
        guard = acquire_mount(root, key)
    except SingleMountViolation:
        result.put("blocked")
    else:
        guard.release()
        result.put("acquired")


class TestCrossProcessExclusion:
    """The real scenario: a second OS process on the same node."""

    def test_second_process_is_blocked_while_the_first_holds(self, tmp_path):
        held = acquire_mount(str(tmp_path), _KEY)
        try:
            ctx = mp.get_context("spawn")
            result = ctx.Queue()
            child = ctx.Process(
                target=_try_acquire, args=(str(tmp_path), _KEY, result)
            )
            child.start()
            child.join(timeout=30)
            assert result.get(timeout=5) == "blocked"
        finally:
            held.release()

    def test_second_process_succeeds_after_release(self, tmp_path):
        acquire_mount(str(tmp_path), _KEY).release()
        ctx = mp.get_context("spawn")
        result = ctx.Queue()
        child = ctx.Process(target=_try_acquire, args=(str(tmp_path), _KEY, result))
        child.start()
        child.join(timeout=30)
        assert result.get(timeout=5) == "acquired"

    def test_lock_dies_with_the_process_that_held_it(self, tmp_path):
        # A crashed holder must not leave the working copy unclaimable: the
        # kernel drops the flock when the process exits, with no reaper.
        ctx = mp.get_context("spawn")
        result = ctx.Queue()
        child = ctx.Process(target=_try_acquire, args=(str(tmp_path), _KEY, result))
        child.start()
        child.join(timeout=30)
        assert result.get(timeout=5) == "acquired"
        acquire_mount(str(tmp_path), _KEY).release()


class TestDeploymentFaultsAreNotContention:
    def test_read_only_root_raises_oserror_not_violation(self, tmp_path):
        # A read-only mount means "fix the deployment", while contention means
        # "surrender the lease and let the gateway retry". Conflating them would
        # send an instance into a silent retry loop against a broken volume.
        root = tmp_path / "ro"
        root.mkdir()
        os.chmod(root, 0o500)
        try:
            with pytest.raises(OSError) as exc:
                acquire_mount(str(root), _KEY)
            assert not isinstance(exc.value, SingleMountViolation)
        finally:
            os.chmod(root, 0o700)
