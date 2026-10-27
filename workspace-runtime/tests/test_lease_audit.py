"""Lease audit tests (design 06 P8 — lease_history write-through).

Two layers, both pinned:

* the manager emits the RIGHT EVENTS at the right lifecycle points (acquire on
  commit, release on every undo path, drain inline before release);
* the auditor's rows are correct against a real database (columns from the
  lease key, UTC timestamps, swallowed failures — an audit write must never
  fail the lease it describes).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.workspace.database as database  # noqa: E402
from src.lease.audit import (  # noqa: E402
    ACTION_ACQUIRE,
    ACTION_DRAIN,
    ACTION_RELEASE,
    LeaseAuditor,
    get_lease_auditor,
)
from src.lease.holder import LeaseHolder  # noqa: E402
from src.lease.manager import LeaseManager, RepositorySource  # noqa: E402

_KEY = "t1/ws-t1-shop/u1/dev"


class _RecordingAuditor:
    """Duck-typed stand-in capturing what the manager emits."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str, str]] = []
        self.awaited: list[tuple[str, str, str]] = []

    def record_soon(self, lease_key: str, action: str, reason: str = "") -> None:
        self.scheduled.append((lease_key, action, reason))

    async def record(self, lease_key: str, action: str, reason: str = "") -> None:
        self.awaited.append((lease_key, action, reason))


def _manager(client, root, remote, auditor=None, **overrides) -> LeaseManager:
    holder = LeaseHolder(
        client=client,
        materialize_root=root,
        runtime_addr="10.0.0.1:8002",
        ttl_seconds=120,
    )

    async def source(lease_key: str) -> RepositorySource:
        return RepositorySource(repo_url=remote)

    kwargs = {
        "holder": holder,
        "materialize_root": root,
        "source_provider": source,
        "wip_interval_seconds": 3600,
        "auditor": auditor,
    }
    kwargs.update(overrides)
    return LeaseManager(**kwargs)


@pytest.fixture()
async def audit_db_async(tmp_path, monkeypatch):
    """A throwaway SQLite database wired into the runtime's session factory."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'lease_audit.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)

    from src.lease.audit import lease_history

    engine = database.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lease_history.create)
    yield
    await database.close_db()


@pytest.fixture()
def git_remote(tmp_path):
    """A bare remote with one seeded model, like the lease-server tests use."""
    from tests.test_lease_server import _git

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
def fake_client():
    from tests.test_lease_server import FakeAsyncRedis

    return FakeAsyncRedis()


class TestManagerEmitsEvents:
    async def test_serve_then_release_schedule_the_pair(
        self, tmp_path, monkeypatch, git_remote, fake_client
    ):
        """The happy path: acquire on commit, release on the explicit call."""
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-a")
        auditor = _RecordingAuditor()
        manager = _manager(fake_client, str(tmp_path / "wc"), git_remote, auditor)

        await manager.serve(_KEY)
        await manager.release(_KEY)

        assert ("t1/ws-t1-shop/u1/dev", ACTION_ACQUIRE, "serve") in auditor.scheduled
        assert ("t1/ws-t1-shop/u1/dev", ACTION_RELEASE, "explicit") in auditor.scheduled

    async def test_drain_audits_inline_before_release(
        self, tmp_path, monkeypatch, git_remote, fake_client
    ):
        """Drain's rows are AWAITED (shutdown path) and precede release_all."""
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-a")
        auditor = _RecordingAuditor()
        manager = _manager(fake_client, str(tmp_path / "wc"), git_remote, auditor)

        await manager.serve(_KEY)
        auditor.scheduled.clear()
        await manager.drain()

        # Awaited inline (not scheduled) — the shutdown path loses nothing.
        assert auditor.awaited == [("t1/ws-t1-shop/u1/dev", ACTION_DRAIN, "drain")]
        assert auditor.scheduled == []

    async def test_no_auditor_means_no_auditing(
        self, tmp_path, monkeypatch, git_remote, fake_client
    ):
        """The manager must work unchanged when auditing is absent (tests,
        older deployments): the duck-typed hook is optional by design."""
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-a")
        manager = _manager(fake_client, str(tmp_path / "wc"), git_remote)

        served = await manager.serve(_KEY)  # must not raise
        assert served.lease_key == _KEY

    async def test_begin_drain_announces_then_drains(
        self, tmp_path, monkeypatch, git_remote, fake_client
    ):
        """The controller's remote entry: draining is visible to placement
        BEFORE the (slow) final snapshots run, and the drain still happens."""
        from datacode_common.lease import runtime_redis_key

        from src.lease.heartbeat import HeartbeatReporter

        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-a")
        auditor = _RecordingAuditor()
        manager = _manager(fake_client, str(tmp_path / "wc"), git_remote, auditor)

        reporter = HeartbeatReporter(
            client=fake_client,
            runtime_id="rt-a",
            addr="10.0.0.1:8002",
            max_leases=16,
            load_provider=lambda: (
                len(manager.served_keys()),
                0.2,
                manager.draining,
                manager.served_keys(),
            ),
        )
        manager.attach_heartbeat(reporter)
        await manager.serve(_KEY)

        # Spy on the announce: what placement would have seen mid-drain.
        announced: list[bool] = []
        original_publish = reporter.publish_once

        async def spying_publish() -> bool:
            announced.append(reporter.current().draining)
            return await original_publish()

        reporter.publish_once = spying_publish

        await manager.begin_drain()

        # The announce carried draining=true while leases were still held.
        assert announced == [True]
        # The drain completed: audit row awaited, nothing still served, and
        # the heartbeat (the placement signal) is gone.
        assert auditor.awaited == [("t1/ws-t1-shop/u1/dev", ACTION_DRAIN, "drain")]
        assert manager.served_keys() == ()
        assert runtime_redis_key("rt-a") not in fake_client._values


class TestAuditorWrites:
    async def test_row_shape_matches_the_migration(self, audit_db_async, monkeypatch):
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-audit")
        auditor = get_lease_auditor()
        before = datetime.now(timezone.utc)

        await auditor.record(_KEY, ACTION_ACQUIRE, "serve")

        from sqlalchemy import select

        from src.lease.audit import lease_history

        async with database.get_session() as session:
            rows = (
                (await session.execute(select(lease_history)))
                .mappings()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        # Columns derived from the lease key, not trusted from the caller.
        assert row["tenant_id"] == "t1"
        assert row["workspace_id"] == "ws-t1-shop"
        assert row["user_id"] == "u1"
        assert row["environment"] == "dev"
        assert row["runtime_id"] == "rt-audit"
        assert row["action"] == ACTION_ACQUIRE
        assert row["reason"] == "serve"
        assert row["occurred_at"] >= before.replace(tzinfo=None)

    async def test_write_failure_never_raises(self, audit_db_async, monkeypatch):
        """The contract that matters most: audit cannot fail the lease."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _broken_session():
            raise RuntimeError("database is unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr("src.lease.audit.get_session", _broken_session)
        auditor = LeaseAuditor()
        # Must not raise despite the broken database.
        await auditor.record(_KEY, ACTION_RELEASE, "explicit")

    async def test_invalid_key_is_skipped_not_raised(self, audit_db_async):
        await get_lease_auditor().record("not-a-key", ACTION_ACQUIRE, "serve")

    async def test_record_soon_schedules_and_swallows(self, audit_db_async):
        get_lease_auditor().record_soon(_KEY, ACTION_ACQUIRE, "serve")
        # Give the scheduled task a chance to run; its exceptions are logged,
        # never propagated to the caller.
        await asyncio.sleep(0.05)
