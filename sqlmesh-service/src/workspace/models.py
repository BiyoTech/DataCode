"""Database models for workspace management.

Defines ORM models for workspaces and user Git credentials.
All timestamps are stored in UTC.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


class WorkspaceModel(Base):
    """Workspace metadata persisted in database."""

    __tablename__ = "workspaces"

    id = Column(String(128), primary_key=True)
    user_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    dialect = Column(String(32), nullable=False, default="duckdb")
    git_repo_url = Column(String(512), nullable=True)
    git_branch = Column(String(64), nullable=False, default="main")
    status = Column(String(32), nullable=False, default="active")
    local_path = Column(String(512), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_active_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_workspaces_user_status", "user_id", "status"),
    )


class UserGitCredentialModel(Base):
    """User's Git provider credentials.

    Supports multiple credentials per user:
    - One OAuth link per provider per user
    - Multiple PAT credentials per provider per user
    """

    __tablename__ = "user_git_credentials"

    id = Column(String(64), primary_key=True)  # UUID
    user_id = Column(String(64), nullable=False)
    provider = Column(String(32), nullable=False)  # github / gitlab / bitbucket / gitea
    nickname = Column(String(128), nullable=False, default="")
    auth_type = Column(String(16), nullable=False, default="pat")  # oauth / pat
    token = Column(Text, nullable=False)  # Encrypted token
    email = Column(String(256), nullable=True)
    username = Column(String(128), nullable=True)
    server_url = Column(String(512), nullable=True)  # Git server URL (for self-hosted)
    is_default = Column(Integer, nullable=False, default=0)  # 1=default, 0=not
    oauth_account = Column(String(256), nullable=True)  # Display name for OAuth
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_git_cred_user_provider", "user_id", "provider"),
        Index("idx_git_cred_user_default", "user_id", "is_default"),
    )


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
    content_type = Column(String(32), nullable=False, default="markdown")  # markdown | text | pdf | csv | json | yaml
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
