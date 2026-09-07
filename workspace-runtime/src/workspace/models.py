"""Database models: this service's own tables plus control-owned views.

Ownership (P5): control-service owns ``workspaces`` and ``user_git_credentials``
— its migrate step (``control-service/src/db_migrate.py``) is the only thing that
creates them, and its REST API performs every write to them. The classes for
those tables below are a READ-ONLY VIEW kept as ORM mappings for the repository
layer, and this service's ``init_db`` deliberately does NOT create them (see
``src.workspace.database``).

Their COLUMNS come from ``datacode_common.models`` rather than being spelled out
again here: two hand-maintained copies of one schema drift, and a drifted column
is not a test failure but data silently truncated. The declarative base stays
local, which is what keeps ``init_db`` able to create this service's three tables
and nothing else.

This service OWNS and creates: ``plan_apply_history``, ``knowledge_documents``,
``file_drafts``.

All timestamps are stored in UTC.
"""

from datetime import datetime, timezone

from datacode_common.models import UserGitCredentialColumns, WorkspaceColumns
from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


class WorkspaceModel(WorkspaceColumns, Base):
    """READ-ONLY view of control-service's ``workspaces`` table.

    Column definitions come from ``datacode_common.models`` so the two services
    cannot drift apart; ownership does not change — this service's ``init_db``
    deliberately never creates this table, and the repository layer only reads
    it.
    """

    __tablename__ = "workspaces"


class UserGitCredentialModel(UserGitCredentialColumns, Base):
    """READ-ONLY view of control-service's ``user_git_credentials`` table."""

    __tablename__ = "user_git_credentials"


class PlanApplyHistoryModel(Base):
    """Audit trail for every `sqlmesh apply` execution.

    Persists the full plan result (changes, backfills, test results) as JSON
    alongside attribution metadata (who triggered the apply, from which session,
    via which channel). All timestamps are stored in UTC.

    Only **apply** operations are recorded (plan previews are read-only and
    never written to history).
    """

    __tablename__ = "plan_apply_history"

    id = Column(String(64), primary_key=True)  # UUIDv4
    workspace_path = Column(String(512), nullable=False)
    environment = Column(String(64), nullable=False, default="dev")

    # Attribution fields (injected by CLI wrapper or frontend)
    user_id = Column(String(64), nullable=False, default="")
    session_id = Column(String(64), nullable=False, default="")
    trigger_type = Column(String(16), nullable=False, default="human")  # human | agent

    # Apply result summary
    applied = Column(Integer, nullable=False, default=0)  # 0=False, 1=True
    reason = Column(String(256), nullable=True)

    # Snapshot counts for list-level display (avoids parsing JSON per row)
    has_changes = Column(Integer, nullable=False, default=0)
    added_count = Column(Integer, nullable=False, default=0)
    removed_count = Column(Integer, nullable=False, default=0)
    direct_count = Column(Integer, nullable=False, default=0)
    indirect_count = Column(Integer, nullable=False, default=0)
    metadata_count = Column(Integer, nullable=False, default=0)
    backfill_count = Column(Integer, nullable=False, default=0)

    # Full plan result serialized as JSON (changes, backfills, test_results)
    plan_result_json = Column(Text, nullable=True)

    # Duration and error tracking
    duration_ms = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
    )

    __table_args__ = (
        Index("idx_apply_history_workspace", "workspace_path", "created_at"),
        Index("idx_apply_history_user", "user_id", "created_at"),
    )


class KnowledgeDocumentModel(Base):
    """Metadata for a business-knowledge document.

    File content is stored on disk under {workspace}/knowledge/; this table
    holds lightweight metadata to enable fast listing/search without scanning
    the filesystem. All timestamps are stored in UTC.
    """

    __tablename__ = "knowledge_documents"

    id = Column(String(64), primary_key=True)  # filename (e.g. "sales_metrics.md")
    workspace_path = Column(String(512), nullable=False)
    title = Column(String(256), nullable=False)
    filename = Column(String(512), nullable=False)  # human-readable, e.g. "sales_metrics.md"
    # markdown | text | pdf | csv | json | yaml
    content_type = Column(String(32), nullable=False, default="markdown")
    file_size = Column(Integer, nullable=False, default=0)  # bytes
    tags = Column(String(512), nullable=True)  # comma-separated tags

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )

    __table_args__ = (
        Index("idx_knowledge_workspace", "workspace_path", "created_at"),
    )


class FileDraftModel(Base):
    """Editor draft autosave — the <=5s layer of the durability contract.

    The working-copy layer (WIP snapshots, ``src.lease.wip``) bounds loss at
    ten minutes for content WRITTEN to the working copy. Drafts bound what was
    typed but not yet saved, and live in the database so they survive the loss
    of a replica's disk entirely (design 07 Q6: "editor keystrokes <= 5 s").

    A draft is deleted the moment its file is saved to the working copy, so at
    most one draft exists per (working copy, file). Table ownership: runtime
    holds it for now; it moves to control-service in P5 together with the rest
    of the workspace metadata (the same path the lease server took).
    """

    __tablename__ = "file_drafts"

    id = Column(String(64), primary_key=True)  # UUID
    workspace_path = Column(String(512), nullable=False)
    file_path = Column(String(512), nullable=False)
    # Drafts hold whole files; TEXT's 64 KiB ceiling is too small for real
    # models, so MySQL gets MEDIUMTEXT (16 MiB). Other dialects use their
    # default large TEXT.
    content = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql"),
        nullable=False,
        default="",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )

    __table_args__ = (
        # Prefix lengths keep the unique index inside MySQL's 3072-byte key
        # limit under utf8mb4 (2 x 255 x 4 = 2040 bytes); SQLite ignores them.
        # Working-copy paths use short platform ids, so 255 chars of prefix
        # cannot collide in practice.
        Index(
            "uq_file_drafts_copy_file",
            "workspace_path",
            "file_path",
            unique=True,
            mysql_length={"workspace_path": 255, "file_path": 255},
        ),
    )
