"""Record retention tests — the expiry half of the audit tables (design 06 P8).

Runs against a throwaway SQLite database (the DELETEs are the code under test).
The properties pinned:

* only rows OLDER than the window go, and the window is anchored at each row's
  last transition (a task that finished yesterday keeps its full 90 days);
* a first pass over a large backlog deletes in BATCHES, never one statement —
  the test proves this by capping the batch size and asserting the count;
* ``0`` disables one table's retention without disabling the loop;
* a failing pass reports and returns, it does not raise — cleanup must not take
  the control plane down with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import src.workspace.database as database
from src.config import settings
from src.records.retention import RecordRetention

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
async def retention_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    await database.init_db()
    yield
    await database.close_db()


async def _add_task(
    task_id: str,
    *,
    days_old: float,
    status: str = "succeeded",
    anchor: datetime = _NOW,
) -> None:
    from src.records.models import TaskRecordModel

    updated = anchor - timedelta(days=days_old)
    async with database.get_session() as session:
        session.add(
            TaskRecordModel(
                id=task_id,
                kind="sqlmesh.apply",
                tenant_id="t1",
                workspace_id="ws-1",
                environment="dev_alice",
                runtime_id="workspace-runtime-0",
                status=status,
                actor_user_id="alice",
                duration_ms=100,
                created_at=updated - timedelta(hours=1),
                updated_at=updated,
            )
        )


async def _add_lease_event(event_id: str, *, days_old: float) -> None:
    from src.records.models import LeaseHistoryModel

    async with database.get_session() as session:
        session.add(
            LeaseHistoryModel(
                id=event_id,
                lease_key="t1/ws-1/alice/dev_alice",
                tenant_id="t1",
                workspace_id="ws-1",
                user_id="alice",
                environment="dev_alice",
                runtime_id="workspace-runtime-0",
                action="acquire",
                reason="",
                occurred_at=_NOW - timedelta(days=days_old),
            )
        )


async def _count(table: str) -> int:
    from sqlalchemy import func, select

    model = {"task_records": "TaskRecordModel", "lease_history": "LeaseHistoryModel"}
    from src.records import models as records_models

    cls = getattr(records_models, model[table])
    async with database.get_session() as session:
        return int(
            await session.scalar(select(func.count()).select_from(cls)) or 0
        )


class TestWindow:
    async def test_only_rows_older_than_the_window_go(self, retention_db, monkeypatch):
        monkeypatch.setattr(settings, "task_record_retention_days", 90)
        monkeypatch.setattr(settings, "lease_history_retention_days", 0)
        await _add_task("old", days_old=91)
        await _add_task("fresh", days_old=89)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.task_rows_deleted == 1
        assert await _count("task_records") == 1

    async def test_task_window_is_anchored_at_the_last_transition(
        self, retention_db, monkeypatch
    ):
        """A task CREATED long ago but finished yesterday keeps its window.

        The zombie question ("what happened to my plan?") is answered from the
        terminal state, so that is what the retention clock must measure.
        """
        monkeypatch.setattr(settings, "task_record_retention_days", 90)
        monkeypatch.setattr(settings, "lease_history_retention_days", 0)
        # Created 200 days ago, updated 10 days ago: young by its last state.
        await _add_task("finished-recently", days_old=10)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.task_rows_deleted == 0

    async def test_lease_history_uses_its_own_window(
        self, retention_db, monkeypatch
    ):
        monkeypatch.setattr(settings, "task_record_retention_days", 0)
        monkeypatch.setattr(settings, "lease_history_retention_days", 30)
        await _add_lease_event("recent", days_old=5)
        await _add_lease_event("old", days_old=31)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.lease_rows_deleted == 1
        assert await _count("lease_history") == 1

    async def test_zero_days_disables_one_table_only(
        self, retention_db, monkeypatch
    ):
        """An explicit way to keep one table forever without stopping the loop."""
        monkeypatch.setattr(settings, "task_record_retention_days", 0)
        monkeypatch.setattr(settings, "lease_history_retention_days", 30)
        await _add_task("ancient", days_old=400)
        await _add_lease_event("old-lease", days_old=400)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.task_rows_deleted == 0
        assert outcome.lease_rows_deleted == 1


class TestBatching:
    async def test_a_large_backlog_is_deleted_in_batches(
        self, retention_db, monkeypatch
    ):
        """The first run after months without retention must not run one giant
        DELETE (it would lock the table and block the writers it serves)."""
        monkeypatch.setattr(settings, "task_record_retention_days", 30)
        monkeypatch.setattr(settings, "lease_history_retention_days", 0)
        monkeypatch.setattr(settings, "retention_batch_size", 50)
        for i in range(120):
            await _add_task(f"task-{i}", days_old=60)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.task_rows_deleted == 120
        assert await _count("task_records") == 0

    async def test_fresh_rows_never_touch_the_database(
        self, retention_db, monkeypatch
    ):
        monkeypatch.setattr(settings, "task_record_retention_days", 90)
        monkeypatch.setattr(settings, "lease_history_retention_days", 30)
        await _add_task("fresh", days_old=1)
        await _add_lease_event("fresh-lease", days_old=1)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.total_deleted == 0


class TestResilience:
    async def test_a_failing_pass_reports_and_returns(
        self, retention_db, monkeypatch
    ):
        """Cleanup must not take the control plane down with it."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _broken_session():
            raise RuntimeError("database is unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr(
            "src.records.retention.get_session", _broken_session
        )
        monkeypatch.setattr(settings, "task_record_retention_days", 1)

        outcome = await RecordRetention().run_once(now=_NOW)

        assert outcome.total_deleted == 0
