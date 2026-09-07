"""Operational record tables landed with P5 (design 06).

* ``task_records`` — the cross-service registry of long-running operations
  (plan/apply/promote). P4b kept the runtime-side tracker process-local; this
  table is where the CONTROL plane archives the outcome so any replica can
  answer "what happened to task X" after the one that ran it is gone.
* ``lease_history`` — append-only audit of lease placement decisions. The live
  lease table lives in Redis (the runtime's placement channel); history lives
  here because it outlives every lease and feeds capacity analytics.

All timestamps are stored in UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from src.workspace.models import Base


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class TaskRecordModel(Base):
    """Outcome of a long-running operation, archived by the control plane."""

    __tablename__ = "task_records"

    id = Column(String(64), primary_key=True)  # task id minted at submission
    kind = Column(String(32), nullable=False)  # plan | apply | promote | ...
    tenant_id = Column(String(64), nullable=False, default="")
    workspace_id = Column(String(128), nullable=False, default="")
    environment = Column(String(40), nullable=False, default="")
    # Which runtime replica executed the task (empty when unknown).
    runtime_id = Column(String(128), nullable=False, default="")
    # pending | running | succeeded | failed | cancelled
    status = Column(String(16), nullable=False, default="pending")
    actor_user_id = Column(String(64), nullable=False, default="")
    # Machine-readable outcome summary (JSON).
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("idx_task_records_workspace", "workspace_id", "created_at"),
        Index("idx_task_records_status", "status", "created_at"),
    )


class LeaseHistoryModel(Base):
    """Append-only audit of lease placement (who held which working copy).

    Written by the lease placement path (today: workspace-runtime's lease
    server; the control plane archives events it observes). One row per
    acquire and per release, correlated by ``lease_key``.
    """

    __tablename__ = "lease_history"

    id = Column(String(64), primary_key=True)  # UUIDv4
    # {tenant}/{workspace}/{user}/{environment} — the lease key.
    lease_key = Column(String(320), nullable=False)
    tenant_id = Column(String(64), nullable=False, default="")
    workspace_id = Column(String(128), nullable=False, default="")
    user_id = Column(String(64), nullable=False, default="")
    environment = Column(String(40), nullable=False, default="")
    runtime_id = Column(String(128), nullable=False, default="")
    # acquire | release | expire | drain | takeover
    action = Column(String(16), nullable=False)
    # Reason detail (e.g. "holder died", "explicit release", "drain").
    reason = Column(String(255), nullable=False, default="")
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("idx_lease_history_key", "lease_key", "occurred_at"),
        Index("idx_lease_history_tenant", "tenant_id", "occurred_at"),
    )
