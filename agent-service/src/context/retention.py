"""Session data retention — the expiry half of the agent schema (design 05 §2.3).

The design table is explicit about hot retention:

* ``timeline_events`` — 30 days (session replay demand is recent by nature)
* ``memory_events`` — 90 days (memory is a longer-lived asset)

Both tables are pure append streams; at 2×10⁶ rows/day for a full cell, the
30-day window alone is 60M rows, which is why the design also prescribes daily
RANGE partitioning (pg_partman, §2.3). Partitioning is a DEPLOYMENT-time
capability that this module deliberately does not depend on:

* with partitions, a cheap ``DROP PARTITION`` would be the ideal mechanism, and
  a row-level DELETE still works (it simply costs more);
* without partitions (dev, compose, the current MySQL/SQLite schema), the
  row-level DELETE is the ONLY implementation.

So this module owns the POLICY (what is expired, anchored when, in what batches)
and leaves the mechanism to plain SQL DELETE, which is correct under both
deployments. The batching rule is the same one control-service's record
retention follows: a first pass over a large backlog must not lock the table in
one transaction.

Why memory_summaries expires WITH its events
--------------------------------------------
A summary describes one event, and outliving it would leave an unanchored
paragraph nobody can navigate to. The FK declares ``ondelete=CASCADE``, but the
retention deletes summaries EXPLICITLY rather than relying on it: SQLite (dev,
tests) does not enforce FKs by default, and the outcome of an expiry pass must
not depend on which backend hosts the schema.

Messages and sessions are deliberately NOT touched
--------------------------------------------------
Messages are the LLM conversation context a user can reopen; sessions carry the
workspace binding. Neither is a high-frequency append stream, and expiring a
session a user still sees in their list would be data loss, not housekeeping.
Their lifecycle belongs to explicit user deletion, not a timer.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.context.database import (
    MemoryEventRow,
    TimelineEventRow,
    get_session_factory,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SessionRetentionOutcome:
    """Result of one pass, for logs, tests and observability."""

    timeline_rows_deleted: int = 0
    memory_rows_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        return self.timeline_rows_deleted + self.memory_rows_deleted


class SessionRetention:
    """Expires timeline events and memory events past their windows."""

    async def run_once(
        self, now: t.Optional[datetime] = None
    ) -> SessionRetentionOutcome:
        """One pass over both tables. Never raises: the next pass retries."""
        moment = now or datetime.now(timezone.utc)
        try:
            timeline = await self._expire_timeline(moment)
            memory = await self._expire_memory(moment)
        except Exception as exc:  # noqa: BLE001 — cleanup must not kill the service
            logger.error("session_retention_failed", error=str(exc))
            return SessionRetentionOutcome()

        outcome = SessionRetentionOutcome(timeline, memory)
        if outcome.total_deleted:
            logger.info(
                "session_retention_completed",
                timeline_rows_deleted=outcome.timeline_rows_deleted,
                memory_rows_deleted=outcome.memory_rows_deleted,
                timeline_retention_days=settings.timeline_retention_days,
                memory_retention_days=settings.memory_event_retention_days,
            )
        return outcome

    async def start_periodic(self, interval_seconds: int) -> None:
        """Run :meth:`run_once` forever, one pass per interval."""
        interval = max(60, interval_seconds)
        logger.info("session_retention_started", interval_seconds=interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("session_retention_stopped")
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("session_retention_cycle_failed", error=str(exc))

    # ── internals ──────────────────────────────────────────────────────

    async def _expire_timeline(self, now: datetime) -> int:
        """Delete timeline events older than their window, in bounded batches.

        Rows are selected by primary key first and deleted by id: DELETE with a
        LIMIT is not portable across the platform's databases, and the batch
        bound keeps the first pass over a months-old backlog from locking the
        table in one transaction.
        """
        days = int(settings.timeline_retention_days)
        if days <= 0:
            return 0
        return await self._expire_model(
            TimelineEventRow,
            TimelineEventRow.created_at,
            TimelineEventRow.id,
            days,
            now,
        )

    async def _expire_memory(self, now: datetime) -> int:
        """Delete memory events older than their window, with their summaries.

        The FK declares ``ondelete=CASCADE``, but relying on it would make the
        outcome depend on the backend: SQLite does not enforce FKs by default,
        so a summary could survive its event as an unanchored paragraph. The
        summaries are therefore deleted explicitly in the same transaction —
        correct under every backend, and harmless where the cascade would have
        removed them anyway.
        """
        days = int(settings.memory_event_retention_days)
        if days <= 0:
            return 0

        from src.context.database import MemorySummaryRow

        cutoff = now - timedelta(days=days)
        batch = max(1, int(settings.retention_batch_size))
        deleted = 0

        while True:
            async with get_session_factory()() as session:
                stale = (
                    (
                        await session.execute(
                            select(MemoryEventRow.id)
                            .where(MemoryEventRow.created_at < cutoff)
                            .order_by(MemoryEventRow.created_at.asc())
                            .limit(batch)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not stale:
                    return deleted

                await session.execute(
                    delete(MemorySummaryRow).where(
                        MemorySummaryRow.event_id.in_(list(stale))
                    )
                )
                result = await session.execute(
                    delete(MemoryEventRow).where(MemoryEventRow.id.in_(list(stale)))
                )
                await session.commit()
                deleted += int(result.rowcount or 0)

    async def _expire_model(
        self,
        model: t.Any,
        timestamp_column: t.Any,
        id_column: t.Any,
        retention_days: int,
        now: datetime,
    ) -> int:
        cutoff = now - timedelta(days=retention_days)
        batch = max(1, int(settings.retention_batch_size))
        deleted = 0

        while True:
            async with get_session_factory()() as session:
                stale = (
                    (
                        await session.execute(
                            select(id_column)
                            .where(timestamp_column < cutoff)
                            .order_by(timestamp_column.asc())
                            .limit(batch)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not stale:
                    return deleted

                try:
                    result = await session.execute(
                        delete(model).where(id_column.in_(list(stale)))
                    )
                    await session.commit()
                except IntegrityError as exc:
                    # A concurrent writer can race the batch (a row selected a
                    # moment ago gained a child). Retry next pass rather than
                    # wedging the loop — the row only gets older, not newer.
                    await session.rollback()
                    logger.warning(
                        "session_retention_batch_conflict",
                        table=model.__tablename__,
                        error=str(exc),
                    )
                    return deleted
                deleted += int(result.rowcount or 0)


_retention: t.Optional[SessionRetention] = None


def get_session_retention() -> SessionRetention:
    """Get or create the shared :class:`SessionRetention` (stateless)."""
    global _retention
    if _retention is None:
        _retention = SessionRetention()
    return _retention


__all__ = ["SessionRetention", "SessionRetentionOutcome", "get_session_retention"]
