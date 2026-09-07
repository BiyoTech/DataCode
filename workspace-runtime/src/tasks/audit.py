"""Task audit — the durable record of what ran in a sandbox (design 06 P6a).

``task_records`` is described by its migration as a cross-service archive: the
runtime's in-memory tracker is per-process by design, so a task that outlived a
pod restart, or that must be explained weeks later, needs a row somewhere.
Several services therefore write it, and this module is the runtime's writer.

Two rules shape everything below.

**Auditing must never change a task's outcome.** A database hiccup while
recording a plan cannot be allowed to fail the plan, so every operation here
swallows its exceptions and logs them. The row is evidence; the task already
happened either way.

**No credentials, no payloads, no user data.** The record holds identifiers,
status, timing and a bounded error message. ``result_json`` keeps the same
pure-data summary the caller received (model names, counts) — never rows,
never SQL — because an audit table is read by more people, and kept for longer,
than the response it describes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from datacode_common.task_protocol import TaskResult, TaskSpec, TaskStatus
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    update,
)

from src.lease.holder import runtime_id
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

# Bound on the stored error text. Long enough to explain a failure, short enough
# that a runaway traceback cannot bloat the table.
MAX_ERROR_CHARS = 4000

# Bound on the serialized result summary, for the same reason.
MAX_RESULT_CHARS = 16000

# Standalone metadata, and a Core table rather than an ORM model: control-service
# OWNS this table (its alembic 0005 creates it). Declaring an ORM model here
# would put a second authoritative definition in the codebase, and the two would
# drift the first time a column changed.
_metadata = MetaData()

task_records = Table(
    "task_records",
    _metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("tenant_id", String(64), nullable=False, server_default=""),
    Column("workspace_id", String(128), nullable=False, server_default=""),
    Column("environment", String(40), nullable=False, server_default=""),
    Column("runtime_id", String(128), nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("actor_user_id", String(64), nullable=False, server_default=""),
    Column("result_json", Text(), nullable=True),
    Column("error_message", Text(), nullable=True),
    Column("duration_ms", Integer(), nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    """UTC, always: the platform compares and stores UTC everywhere."""
    return datetime.now(timezone.utc)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _summarize(result: TaskResult) -> Optional[str]:
    """Serialize the result summary for storage, or None when there is nothing.

    Serialization failures degrade to a note rather than propagating: an
    unserializable result is a reason to fix a handler, not to lose the record
    that the task ran.
    """
    if not result.result:
        return None
    try:
        payload: Any = dict(result.result)
        return _truncate(json.dumps(payload, default=str), MAX_RESULT_CHARS)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "task_audit_result_unserializable",
            task_id=result.task_id,
            error=str(exc),
        )
        return json.dumps({"note": "result summary could not be serialized"})


class TaskAuditor:
    """Writes the lifecycle of a task to ``task_records``."""

    async def record_submitted(self, spec: TaskSpec, *, backend: str) -> None:
        """Insert the row for a task about to run.

        Written BEFORE execution on purpose: a runtime that dies mid-task then
        leaves a ``pending`` row, which is what makes the loss visible. Inserting
        afterwards would make a crashed task indistinguishable from one that was
        never submitted.
        """
        now = _now()
        values = {
            "id": spec.task_id,
            "kind": spec.kind,
            "tenant_id": spec.tenant_id,
            "workspace_id": spec.workspace_id,
            "environment": spec.environment,
            "runtime_id": runtime_id(),
            "status": TaskStatus.PENDING.value,
            "actor_user_id": spec.actor_user_id,
            "duration_ms": 0,
            "created_at": now,
            "updated_at": now,
        }
        try:
            async with get_session() as session:
                await session.execute(insert(task_records).values(**values))
        except Exception as exc:  # noqa: BLE001 — auditing must not fail a task
            logger.warning(
                "task_audit_insert_failed",
                task_id=spec.task_id,
                kind=spec.kind,
                backend=backend,
                error=str(exc),
            )

    async def record_finished(self, spec: TaskSpec, result: TaskResult) -> None:
        """Update the row with the terminal outcome."""
        values: dict[str, Any] = {
            "status": result.status.value,
            "duration_ms": int(result.duration_ms or 0),
            "result_json": _summarize(result),
            "updated_at": _now(),
        }
        if result.error_message or result.error_code:
            # The code is prefixed into the message rather than given its own
            # column: the table is shared, and adding a column to it belongs to
            # the service that owns the migration.
            prefix = f"[{result.error_code}] " if result.error_code else ""
            values["error_message"] = _truncate(
                prefix + (result.error_message or ""), MAX_ERROR_CHARS
            )

        try:
            async with get_session() as session:
                await session.execute(
                    update(task_records)
                    .where(task_records.c.id == spec.task_id)
                    .values(**values)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "task_audit_update_failed",
                task_id=spec.task_id,
                status=result.status.value,
                error=str(exc),
            )


_auditor: Optional[TaskAuditor] = None


def get_task_auditor() -> TaskAuditor:
    """Return the shared auditor."""
    global _auditor
    if _auditor is None:
        _auditor = TaskAuditor()
    return _auditor


__all__ = [
    "MAX_ERROR_CHARS",
    "MAX_RESULT_CHARS",
    "TaskAuditor",
    "get_task_auditor",
    "task_records",
]
