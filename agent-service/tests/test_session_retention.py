"""Session retention tests — the expiry half of the agent schema (design 05 §2.3).

Runs against a throwaway SQLite database. Pinned properties:

* each table keeps only its own window (30 days timeline, 90 days memory);
* expiring a memory event CASCADES to its summary (a summary must not outlive
  the event it describes);
* messages and sessions are never touched by the timer — their lifecycle belongs
  to explicit user deletion, and expiring a session the user still sees in their
  list would be data loss, not housekeeping;
* a large backlog is deleted in batches, never one statement;
* a failing pass reports and returns rather than raising.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import src.context.database as database
from src.config import settings
from src.context.database import (
    MemoryEventRow,
    MemorySummaryRow,
    MessageRow,
    SessionRow,
    TimelineEventRow,
)
from src.context.retention import SessionRetention

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
_SESSION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
async def agent_db(tmp_path, monkeypatch):
    """A throwaway SQLite database wired into the session factory.

    ``database_url`` is a computed property over the platform config, so the
    injection point is the engine FACTORY the module calls: it receives the
    settings-derived URL and ignores it, handing back an engine for the
    throwaway file instead.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}"
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)

    def _engine_for_url(_url: str, **_kwargs):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(url)

    monkeypatch.setattr(database, "create_async_engine_for_url", _engine_for_url)
    await database.init_db()
    # One session everything hangs off; it must survive every pass below.
    async with database.get_session_factory()() as db:
        db.add(
            SessionRow(
                id=_SESSION, user_id="alice", title="t", mode="ask", status="active"
            )
        )
        await db.commit()
    yield
    await database.close_db()


async def _timeline(event_id: str, *, days_old: float) -> None:
    async with database.get_session_factory()() as db:
        db.add(
            TimelineEventRow(
                session_id=_SESSION,
                event_type="tool.start",
                event_id=event_id,
                payload_json="{}",
                created_at=_NOW - timedelta(days=days_old),
            )
        )
        await db.commit()


async def _memory(event_id: str, *, days_old: float, with_summary: bool) -> None:
    created = _NOW - timedelta(days=days_old)
    async with database.get_session_factory()() as db:
        event = MemoryEventRow(
            id=event_id,
            workspace_path="/base/t1/ws-1",
            session_id=_SESSION,
            event_key=f"key-{event_id}",
            event_type="action",
            created_at=created,
        )
        db.add(event)
        if with_summary:
            db.add(
                MemorySummaryRow(
                    id=str(uuid.uuid4()),
                    event_id=event_id,
                    workspace_path="/base/t1/ws-1",
                    summary="what happened",
                    context_json="{}",
                    created_at=created,
                )
            )
        await db.commit()


async def _message(*, days_old: float) -> None:
    async with database.get_session_factory()() as db:
        db.add(
            MessageRow(
                session_id=_SESSION,
                role="user",
                content="hello",
                metadata_json="{}",
                created_at=_NOW - timedelta(days=days_old),
            )
        )
        await db.commit()


async def _count(model) -> int:
    from sqlalchemy import func, select

    async with database.get_session_factory()() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


class TestWindows:
    async def test_timeline_keeps_only_its_window(self, agent_db, monkeypatch):
        monkeypatch.setattr(settings, "timeline_retention_days", 30)
        monkeypatch.setattr(settings, "memory_event_retention_days", 0)
        await _timeline("old", days_old=31)
        await _timeline("recent", days_old=29)

        outcome = await SessionRetention().run_once(now=_NOW)

        assert outcome.timeline_rows_deleted == 1
        assert await _count(TimelineEventRow) == 1

    async def test_memory_keeps_only_its_window(self, agent_db, monkeypatch):
        monkeypatch.setattr(settings, "timeline_retention_days", 0)
        monkeypatch.setattr(settings, "memory_event_retention_days", 90)
        await _memory("recent", days_old=89, with_summary=True)
        await _memory("old", days_old=91, with_summary=True)

        outcome = await SessionRetention().run_once(now=_NOW)

        assert outcome.memory_rows_deleted == 1
        assert await _count(MemoryEventRow) == 1

    async def test_memory_expiry_cascades_to_summaries(self, agent_db, monkeypatch):
        """A summary describes one event; outliving it would leave an
        unanchored paragraph nobody can navigate to."""
        monkeypatch.setattr(settings, "timeline_retention_days", 0)
        monkeypatch.setattr(settings, "memory_event_retention_days", 90)
        await _memory("old", days_old=120, with_summary=True)
        assert await _count(MemorySummaryRow) == 1

        await SessionRetention().run_once(now=_NOW)

        assert await _count(MemoryEventRow) == 0
        assert await _count(MemorySummaryRow) == 0

    async def test_messages_and_sessions_are_never_expired(self, agent_db, monkeypatch):
        """Their lifecycle belongs to explicit user deletion, not a timer."""
        monkeypatch.setattr(settings, "timeline_retention_days", 30)
        monkeypatch.setattr(settings, "memory_event_retention_days", 90)
        await _message(days_old=400)
        await _timeline("old", days_old=400)

        await SessionRetention().run_once(now=_NOW)

        assert await _count(MessageRow) == 1
        assert await _count(SessionRow) == 1
        assert await _count(TimelineEventRow) == 0

    async def test_zero_days_disables_one_table_only(self, agent_db, monkeypatch):
        monkeypatch.setattr(settings, "timeline_retention_days", 0)
        monkeypatch.setattr(settings, "memory_event_retention_days", 90)
        await _timeline("ancient", days_old=400)
        await _memory("old", days_old=400, with_summary=False)

        outcome = await SessionRetention().run_once(now=_NOW)

        assert outcome.timeline_rows_deleted == 0
        assert outcome.memory_rows_deleted == 1


class TestBatching:
    async def test_a_large_backlog_is_deleted_in_batches(
        self, agent_db, monkeypatch
    ):
        monkeypatch.setattr(settings, "timeline_retention_days", 30)
        monkeypatch.setattr(settings, "memory_event_retention_days", 0)
        monkeypatch.setattr(settings, "retention_batch_size", 40)
        for i in range(90):
            await _timeline(f"ev-{i}", days_old=60)

        outcome = await SessionRetention().run_once(now=_NOW)

        assert outcome.timeline_rows_deleted == 90
        assert await _count(TimelineEventRow) == 0


class TestResilience:
    async def test_a_failing_pass_reports_and_returns(
        self, agent_db, monkeypatch
    ):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _broken_factory():
            raise RuntimeError("database is unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr(
            "src.context.retention.get_session_factory", _broken_factory
        )
        monkeypatch.setattr(settings, "timeline_retention_days", 1)

        outcome = await SessionRetention().run_once(now=_NOW)

        assert outcome.total_deleted == 0
