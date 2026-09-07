"""Task record queries — the reconciler's zombie-task input (design 05 §5.1).

``task_records`` is written by whoever runs a task (workspace-runtime's
``src.tasks.audit``) and read here by the party that can compare it against the
cluster. This module holds only the reads and the ONE terminal write the
reconciler is allowed to make.

Why a young task is never a zombie
----------------------------------
A ``pending`` row is written BEFORE the vehicle exists — that ordering is what
makes a crashed submission visible at all. So for a short window every healthy
task legitimately has a row and no Pod. Querying with a grace period, rather
than comparing instantaneously, is what separates "starting" from "lost"; the
window belongs to the caller, because only it knows how long a vehicle takes to
appear in this deployment.

The reconciler's write is deliberately narrow: mark a lost task ``failed``, with
a reason, and only while the row is still in a non-terminal state. A task that
finished between the read and the write keeps its real outcome.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.records.models import TaskRecordModel
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

# States a task can still leave on its own. Everything else is terminal and must
# never be rewritten by reconciliation.
NON_TERMINAL_STATUSES = ("pending", "running")

STATUS_FAILED = "failed"

# Bound on the stored reason, matching the column's own budget.
_MAX_REASON_CHARS = 2000


@dataclass(frozen=True)
class UnfinishedTask:
    """One task the registry believes is still in flight."""

    task_id: str
    kind: str
    tenant_id: str
    workspace_id: str
    environment: str
    runtime_id: str
    status: str
    created_at: datetime

    def age_seconds(self, *, now: t.Optional[datetime] = None) -> float:
        """How long this task has claimed to be running."""
        reference = now or datetime.now(timezone.utc)
        return (reference - self.created_at).total_seconds()


class TaskRecordRepository:
    """Reads unfinished tasks and settles the ones that were lost."""

    async def list_unfinished(
        self,
        *,
        workspace_id: str = "",
        older_than_seconds: int = 0,
        now: t.Optional[datetime] = None,
    ) -> t.List[UnfinishedTask]:
        """Tasks still claiming to run, optionally scoped and aged.

        ``older_than_seconds`` excludes rows too young to judge (see the module
        docstring); ``workspace_id`` scopes the query, which is what the
        per-workspace reconciliation pass needs.
        """
        reference = now or datetime.now(timezone.utc)
        conditions = [TaskRecordModel.status.in_(list(NON_TERMINAL_STATUSES))]
        if workspace_id:
            conditions.append(TaskRecordModel.workspace_id == workspace_id)
        if older_than_seconds > 0:
            conditions.append(
                TaskRecordModel.created_at
                < reference - timedelta(seconds=older_than_seconds)
            )

        async with get_session() as session:
            result = await session.execute(
                select(TaskRecordModel)
                .where(*conditions)
                .order_by(TaskRecordModel.created_at.asc())
            )
            rows = list(result.scalars().all())

        return [
            UnfinishedTask(
                task_id=row.id,
                kind=row.kind,
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
                environment=row.environment,
                runtime_id=row.runtime_id,
                status=row.status,
                created_at=_as_utc(row.created_at) or reference,
            )
            for row in rows
        ]

    async def mark_failed(self, task_id: str, reason: str) -> bool:
        """Settle a lost task as ``failed``; returns whether the row changed.

        Compare-and-set on the non-terminal statuses: the read that decided this
        task was lost is necessarily older than this write, and a task that
        reported its own outcome in between must keep it. False therefore means
        "it settled itself", which is a success for the caller, not an error.
        """
        async with get_session() as session:
            result = await session.execute(
                update(TaskRecordModel)
                .where(
                    TaskRecordModel.id == task_id,
                    TaskRecordModel.status.in_(list(NON_TERMINAL_STATUSES)),
                )
                .values(
                    status=STATUS_FAILED,
                    error_message=reason[:_MAX_REASON_CHARS],
                    updated_at=datetime.now(timezone.utc),
                )
            )
            changed = result.rowcount > 0

        logger.info(
            "zombie_task_settled" if changed else "zombie_task_settled_meanwhile",
            task_id=task_id,
        )
        return changed


def _as_utc(value: t.Optional[datetime]) -> t.Optional[datetime]:
    """Interpret a stored timestamp as UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns,
    and comparing those against an aware "now" raises. Everything the platform
    stores is UTC by contract, so a naive value is labelled as such — the same
    helper the reaper needs, for the same reason.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_repository: t.Optional[TaskRecordRepository] = None


def get_task_record_repository() -> TaskRecordRepository:
    """Get or create the global :class:`TaskRecordRepository` (stateless)."""
    global _repository
    if _repository is None:
        _repository = TaskRecordRepository()
    return _repository


__all__ = [
    "NON_TERMINAL_STATUSES",
    "STATUS_FAILED",
    "TaskRecordRepository",
    "UnfinishedTask",
    "get_task_record_repository",
]
