"""Lease audit — the durable record of who held which working copy.

``lease_history`` was landed by control-service's 0005 migration as an
append-only archive of lease placement decisions: one row per acquire, release
and drain, correlated by ``lease_key``. The events all happen HERE (the lease
lifecycle runs on the runtime), so this module is the runtime's writer — the
same cross-service pattern ``task_records`` uses (see ``src.tasks.audit``).

Two rules, inherited from that precedent:

**Auditing must never change a lease's outcome.** A database hiccup while
recording an acquire must not fail the acquire, so every write swallows its
exceptions and logs them. The row is evidence; the lease already happened.

**The request path must not wait for the audit.** Acquire and release run on
the gateway's path; their rows are written in a SCHEDULED task, not awaited.
Crash-window loss is accepted (a handful of rows from an instance that died
mid-write) because the history feeds capacity analytics, not correctness — the
lease itself lives in Redis. The drain event is the exception: it is written
inline, because drain is the shutdown path whose whole point is that nothing
is lost.

No credentials, no payload data: identifiers, action, reason and UTC time.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from datacode_common.naming import split_lease_key
from sqlalchemy import Column, DateTime, MetaData, String, Table, insert

from src.lease.holder import runtime_id
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

# Standalone metadata and a Core table, NOT an ORM model: control-service owns
# this table (its alembic 0005 creates it). A second authoritative definition
# here would drift the first time a column changed on one side only.
_metadata = MetaData()

lease_history = Table(
    "lease_history",
    _metadata,
    Column("id", String(64), primary_key=True),
    Column("lease_key", String(320), nullable=False),
    Column("tenant_id", String(64), nullable=False, server_default=""),
    Column("workspace_id", String(128), nullable=False, server_default=""),
    Column("user_id", String(64), nullable=False, server_default=""),
    Column("environment", String(40), nullable=False, server_default=""),
    Column("runtime_id", String(128), nullable=False, server_default=""),
    Column("action", String(16), nullable=False),
    Column("reason", String(255), nullable=False, server_default=""),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

# The vocabulary the migration documents. Kept as plain constants rather than
# an Enum: the column is shared with control-service's readers, and a drifted
# enum NAME on one side must never change what gets STORED.
ACTION_ACQUIRE = "acquire"
ACTION_RELEASE = "release"
ACTION_DRAIN = "drain"


def _now() -> datetime:
    """UTC, always: the platform stores and compares UTC everywhere."""
    return datetime.now(timezone.utc)


class LeaseAuditor:
    """Writes lease lifecycle events to ``lease_history``."""

    async def record(self, lease_key: str, action: str, reason: str = "") -> None:
        """Insert one event row. Never raises: see the module docstring."""
        try:
            tenant_id, workspace_id, user_id, environment = split_lease_key(lease_key)
        except Exception as exc:  # noqa: BLE001 — a bad key is a log line, not a crash
            logger.warning(
                "lease_audit_key_invalid",
                lease_key=lease_key,
                action=action,
                error=str(exc),
            )
            return

        try:
            async with get_session() as session:
                await session.execute(
                    insert(lease_history).values(
                        id=str(uuid.uuid4()),
                        lease_key=lease_key,
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        user_id=user_id,
                        environment=environment,
                        runtime_id=runtime_id(),
                        action=action,
                        reason=reason[:255],
                        occurred_at=_now(),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — audit must not fail the lease
            logger.warning(
                "lease_audit_write_failed",
                lease_key=lease_key,
                action=action,
                error=str(exc),
            )

    def record_soon(self, lease_key: str, action: str, reason: str = "") -> None:
        """Schedule :meth:`record` off the request path.

        The task is not tracked beyond its own error logging: it is a
        fire-and-forget write whose loss on a crash is an accepted, documented
        trade (see the module docstring).
        """
        task = asyncio.create_task(
            self.record(lease_key, action, reason),
            name=f"lease-audit-{action}-{lease_key}",
        )
        task.add_done_callback(_log_task_failure)


def _log_task_failure(task: asyncio.Task) -> None:
    """An unobserved task exception produces no log line; this one does."""
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        logger.error("lease_audit_task_failed", error=str(exc))


_auditor: Optional[LeaseAuditor] = None


def get_lease_auditor() -> LeaseAuditor:
    """Return the shared auditor."""
    global _auditor
    if _auditor is None:
        _auditor = LeaseAuditor()
    return _auditor
