"""Record retention — the cleanup half of the audit tables (design 06 P8).

``task_records`` and ``lease_history`` are APPEND-ONLY by contract: nothing in
this module ever rewrites a row, it only deletes whole rows older than the
retention window. The 0005 migration that created them carried no expiry, so
without this loop both tables grow monotonically — and the reconciler's zombie
scan (which queries non-terminal task rows) is exactly the query an unbounded
table eventually makes slow.

Why retention lives with the reader
------------------------------------
control-service owns these tables' migrations, but the runtime writes them; the
cleanup is placed HERE because only the control plane runs cell-wide periodic
jobs (the reaper, the sweep, now this). One writer more would not violate the
ownership rule — deleting rows is not schema evolution — but one OWNER of the
lifecycle is what keeps "how long do we keep this?" from being answered
differently per service.

Why the counts are bounded per pass
-----------------------------------
A first deployment enabling retention after months without it must not attempt
one giant DELETE in a single transaction: that locks the table, blows the undo
log and blocks the writers it is meant to serve. The pass deletes in bounded
batches until the window is clear.

Why lease_history keeps a shorter tail than task_records
--------------------------------------------------------
task_records answers "what happened to my plan/apply" — user-facing, and the
design's retention is 90 days. lease_history feeds capacity analytics
(hand-over patterns, holder lifetime), for which 30 days is ample; it also grows
faster (multiple events per lease lifetime), so its ceiling is what actually
protects the table size.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    delete,
    select,
)

from src.config import settings
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

# Mirrors of the audit tables (control owns them via the 0005 migration; the
# runtime writes them through the same Core-Table discipline).
_metadata = MetaData()

_task_records = Table(
    "task_records",
    _metadata,
    Column("id", String(64), primary_key=True),
    Column("status", String(16)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

_lease_history = Table(
    "lease_history",
    _metadata,
    Column("id", String(64), primary_key=True),
    Column("occurred_at", DateTime(timezone=True)),
)


@dataclass(frozen=True)
class RetentionOutcome:
    """Result of one retention pass, for logs, tests and the manual endpoint."""

    task_rows_deleted: int = 0
    lease_rows_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        return self.task_rows_deleted + self.lease_rows_deleted


class RecordRetention:
    """Deletes audit rows older than their retention windows, in batches."""

    async def run_once(self, now: t.Optional[datetime] = None) -> RetentionOutcome:
        """One pass over both tables. Never raises: cleanup must not take the
        control plane down with it, and the next pass retries whatever failed."""
        moment = now or datetime.now(timezone.utc)
        outcome = RetentionOutcome()

        try:
            outcome = RetentionOutcome(
                task_rows_deleted=await self._expire(
                    _task_records,
                    _task_records.c.updated_at,
                    # A row's life ends at its LAST transition, not creation: a
                    # task that ran last week still has its full window from
                    # when it finished.
                    settings.task_record_retention_days,
                    moment,
                ),
                lease_rows_deleted=await self._expire(
                    _lease_history,
                    _lease_history.c.occurred_at,
                    settings.lease_history_retention_days,
                    moment,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — the tables stay; only this pass is lost
            logger.error("record_retention_failed", error=str(exc))
            return outcome

        if outcome.total_deleted:
            logger.info(
                "record_retention_completed",
                task_rows_deleted=outcome.task_rows_deleted,
                lease_rows_deleted=outcome.lease_rows_deleted,
                task_retention_days=settings.task_record_retention_days,
                lease_retention_days=settings.lease_history_retention_days,
            )
        return outcome

    async def start_periodic(self, interval_seconds: int) -> None:
        """Run :meth:`run_once` forever, one pass per interval."""
        interval = max(60, interval_seconds)
        logger.info("record_retention_started", interval_seconds=interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("record_retention_stopped")
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("record_retention_cycle_failed", error=str(exc))

    # ── internals ──────────────────────────────────────────────────────

    async def _expire(
        self,
        table: Table,
        timestamp_column,
        retention_days: int,
        now: datetime,
    ) -> int:
        """Delete rows older than the window, in bounded batches.

        Returns the total number of rows removed. ``retention_days <= 0`` means
        "keep everything" — an explicit way to disable one table's retention
        without disabling the loop (the other table may still need it).
        """
        if retention_days <= 0:
            return 0

        cutoff = now - timedelta(days=retention_days)
        batch = max(1, int(settings.retention_batch_size))
        deleted = 0

        while True:
            # DELETE with a LIMIT is not portable across the platform's
            # databases (MySQL supports it, SQLite and Postgres do not), so the
            # batch is selected first and deleted by primary key. The subquery
            # is indexed by the timestamp column of the table it reads.
            async with get_session() as session:
                stale_ids = (
                    (
                        await session.execute(
                            select(table.c.id)
                            .where(timestamp_column < cutoff)
                            .order_by(timestamp_column.asc())
                            .limit(batch)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not stale_ids:
                    return deleted
                result = await session.execute(
                    delete(table).where(table.c.id.in_(list(stale_ids)))
                )
                await session.commit()
                deleted += int(result.rowcount or 0)

            logger.debug(
                "record_retention_batch",
                table=table.name,
                rows=int(result.rowcount or 0),
                cutoff=cutoff.isoformat(),
            )


_retention: t.Optional[RecordRetention] = None


def get_record_retention() -> RecordRetention:
    """Get or create the global :class:`RecordRetention` (stateless)."""
    global _retention
    if _retention is None:
        _retention = RecordRetention()
    return _retention


__all__ = ["RecordRetention", "RetentionOutcome", "get_record_retention"]
