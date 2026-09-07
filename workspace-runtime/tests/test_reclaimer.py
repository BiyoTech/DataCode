"""Disk reclaimer tests (design 06 4b).

Run against REAL git (a local bare repo as the remote, as test_materialize
does), because the property that matters end-to-end is what a reclaim leaves
behind: the worktree gone, the shared ``.repo`` intact and still able to
re-materialize the environment cheaply.

The watermark itself is faked (injectable ``disk_usage``): the point is the
DECISION — which victims, in which order, stopping where — not arithmetic on
``statvfs``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lease.materialize import (  # noqa: E402
    discard,
    is_valid_bare_repo,
    is_valid_worktree,
    materialize,
)
from src.lease.reclaimer import DiskReclaimer  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for reclaimer tests"
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
    """A bare repository standing in for the Git server (env/dev branch)."""
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
def root(tmp_path: Path) -> str:
    materialize_root = tmp_path / "workcopies"
    materialize_root.mkdir()
    return str(materialize_root)


class FakeUsage:
    """Scriptable filesystem usage for watermark tests."""

    def __init__(self) -> None:
        self.percent = 0.0

    def __call__(self, _path: str) -> tuple[float, float]:
        return self.percent, 100.0


def _reclaimer(
    root: str,
    *,
    usage: FakeUsage,
    served: set[str] = frozenset(),  # type: ignore[assignment]
    last_used: dict[str, float] | None = None,
    high: float = 85.0,
    low: float = 70.0,
) -> DiskReclaimer:
    return DiskReclaimer(
        materialize_root=root,
        served_keys=lambda: served,  # type: ignore[misc]
        last_used=lambda: last_used or {},
        high_watermark_percent=high,
        low_watermark_percent=low,
        interval_seconds=3600,
        disk_usage=usage,
    )


class TestWatermarkDecisions:
    def test_noop_below_high_watermark(self, root, remote):
        materialize(materialize_root=root, lease_key="t1/ws/u1/dev", repo_url=remote)
        usage = FakeUsage()
        usage.percent = 50.0

        reclaimed = _reclaimer(root, usage=usage).reclaim_once()

        assert reclaimed == []
        assert is_valid_worktree(str(Path(root) / "t1" / "ws" / "u1" / "envs" / "dev"))

    def test_reclaims_least_recently_used_until_low_watermark(
        self, root, remote
    ):
        old = materialize(
            materialize_root=root, lease_key="t1/ws/u1/old", repo_url=remote
        )
        fresh = materialize(
            materialize_root=root, lease_key="t1/ws/u1/new", repo_url=remote
        )
        assert is_valid_worktree(old.workspace_path)
        assert is_valid_worktree(fresh.workspace_path)

        usage = FakeUsage()
        # Above high on entry; drops to the low watermark only after ONE
        # worktree is reclaimed (the scripted scenario under test).
        usage.percent = 90.0
        now = time.time()
        reclaimer = _reclaimer(
            root,
            usage=usage,
            last_used={
                "t1/ws/u1/old": now - 3600.0,
                "t1/ws/u1/new": now,
            },
        )
        # The usage drop happens as soon as one copy is gone.
        original_reclaim = reclaimer.used_percent

        def _drop_after_first_reclaim() -> float:
            if not is_valid_worktree(old.workspace_path):
                usage.percent = 60.0
            return original_reclaim()

        reclaimer.used_percent = _drop_after_first_reclaim  # type: ignore[assignment]

        reclaimed = reclaimer.reclaim_once()

        assert reclaimed == ["t1/ws/u1/old"]
        assert not is_valid_worktree(old.workspace_path)
        assert is_valid_worktree(fresh.workspace_path)

    def test_served_copies_are_never_victims(self, root, remote):
        served_key = "t1/ws/u1/served"
        copy = materialize(materialize_root=root, lease_key=served_key, repo_url=remote)
        usage = FakeUsage()
        usage.percent = 95.0

        reclaimed = _reclaimer(
            root, usage=usage, served={served_key}
        ).reclaim_once()

        # Nothing else to reclaim: above the low line with no candidates is a
        # capacity fact, and the served copy must stay untouched either way.
        assert copy.lease_key not in reclaimed
        assert is_valid_worktree(copy.workspace_path)

    def test_repo_survives_reclaim_and_rematerializes(self, root, remote):
        """The whole point of keeping .repo: reclaiming an environment must not
        cost its user's other environments their object database, and a
        re-lease of the reclaimed environment must not re-clone."""
        key = "t1/ws/u1/dev"
        first = materialize(materialize_root=root, lease_key=key, repo_url=remote)
        discard(root, key)
        assert not is_valid_worktree(first.workspace_path)

        # No repo_url on purpose: only a still-valid .repo can satisfy this.
        again = materialize(materialize_root=root, lease_key=key)
        assert again.freshly_materialized is True  # worktree was rebuilt...
        assert is_valid_bare_repo(again.repo_path)  # ...from the KEPT objects.


class TestDiscoveryFallbacks:
    def test_unknown_stamps_fall_back_to_mtime(self, root, remote):
        old = materialize(
            materialize_root=root, lease_key="t1/ws/u1/old", repo_url=remote
        )
        newer = materialize(
            materialize_root=root, lease_key="t1/ws/u1/new", repo_url=remote
        )
        # No in-memory stamps at all (the post-restart situation): directory
        # mtime decides. Force the order by backdating `old`.
        past = time.time() - 7200.0
        os.utime(old.workspace_path, (past, past))
        os.utime(newer.workspace_path, None)

        usage = FakeUsage()
        usage.percent = 90.0
        reclaimer = _reclaimer(root, usage=usage)  # last_used -> {}

        candidates = reclaimer._candidates(exclude=set())
        assert [key for key, _stamp in candidates[:2]] == [
            "t1/ws/u1/old",
            "t1/ws/u1/new",
        ]

    def test_ignores_directories_outside_the_layout(self, root, tmp_path):
        """Only the materializer's layout is visible; random directories on
        the volume are somebody else's data and must never be reclaimed."""
        (Path(root) / "not-a-tenant" / "junk").mkdir(parents=True)
        usage = FakeUsage()
        usage.percent = 99.0

        reclaimed = _reclaimer(root, usage=usage).reclaim_once()

        assert reclaimed == []
        assert (Path(root) / "not-a-tenant" / "junk").is_dir()
