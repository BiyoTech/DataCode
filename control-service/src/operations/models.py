"""``workspace_operations`` — one row per create/delete of a workspace.

Owned by control-service (created by ``create_all`` and the 0006 migration).
All timestamps are UTC. ``detail`` carries identifiers and step facts only —
never credentials or remote URLs, which may embed a token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from src.workspace.models import Base


def _utc_now() -> datetime:
    """Current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class WorkspaceOperationModel(Base):
    """Progress of one workspace lifecycle operation."""

    __tablename__ = "workspace_operations"

    op_id = Column(String(64), primary_key=True)  # UUIDv4 hex
    kind = Column(String(16), nullable=False)  # create | delete
    workspace_id = Column(String(128), nullable=False)
    tenant_id = Column(String(64), nullable=False)
    actor_user_id = Column(String(64), nullable=False, default="")
    # Last step whose effect has been ACHIEVED (see the recorder for names).
    step = Column(String(32), nullable=False, default="begun")
    # running | failed | succeeded | rolled_back | superseded | abandoned
    status = Column(String(16), nullable=False, default="running")
    # Resume attempts made by the sweep (the original run is attempt 0).
    attempts = Column(Integer, nullable=False, default=0)
    # JSON object of step facts, e.g. {"final_step": "pushed"}.
    detail = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        # The resumer's query: non-terminal rows that have not moved recently.
        Index("idx_workspace_operations_status", "status", "updated_at"),
        # "Is there a newer operation on this workspace?"
        Index("idx_workspace_operations_workspace", "workspace_id", "created_at"),
    )
