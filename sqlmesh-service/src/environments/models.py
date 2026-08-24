"""Database models for the Environment registry.

The registry is the SOURCE OF TRUTH: Git branches, SQLMesh state entries and
Dagster code locations are reconciled FROM these tables, never the reverse.

Models are attached to the same declarative ``Base`` as the workspace models so
one metadata object covers the whole service schema.
All timestamps are stored in UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from src.workspace.models import Base


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class EnvironmentModel(Base):
    """A platform environment: one Git branch + one SQLMesh env + one deployment.

    Scoped to a WORKSPACE, not to a user: environments are shared team assets,
    which is what makes promotion and diffing between them meaningful. Each
    member materializes their own Git worktree of the environment's branch, and
    that path is always derived at runtime (never stored here).
    """

    __tablename__ = "environments"

    # {workspace_id}:{name} - see naming.environment_id()
    id = Column(String(128), primary_key=True)
    workspace_id = Column(String(128), nullable=False)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(40), nullable=False)      # naming.ENV_NAME_RE
    branch = Column(String(128), nullable=False)   # derived: naming.branch_of(name)

    # production | shared | sandbox
    env_type = Column(String(16), nullable=False, default="sandbox")
    # Base environment for diff / promote / create_from
    base_env = Column(String(40), nullable=False, default="prod")

    owner_user_id = Column(String(64), nullable=False)

    # creating | active | applying | promoting | drifted | archiving | archived | failed
    status = Column(String(16), nullable=False, default="creating")

    # Dagster deployment binding (bound in Phase 4; name is derivable and frozen
    # at creation time so the two sides can never disagree).
    deployment_name = Column(String(160), nullable=False, default="")
    schedules_enabled = Column(Integer, nullable=False, default=0)

    # Last successful apply / promote markers. ``last_applied_commit`` compared
    # against the branch HEAD is what surfaces "code changed, data did not".
    last_applied_commit = Column(String(64), nullable=True)
    last_applied_at = Column(DateTime(timezone=True), nullable=True)
    last_promoted_to = Column(String(40), nullable=True)
    # 1 when the last apply ran from a working copy with uncommitted changes.
    # The data is then NOT attributable to ``last_applied_commit``, so the UI
    # must report drift even though the commit matches the branch HEAD.
    last_applied_dirty = Column(Integer, nullable=False, default=0)

    # Lifecycle: sandbox environments expire, production never does.
    ttl_hours = Column(Integer, nullable=False, default=168)  # 7 days
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_env_workspace_name"),
        UniqueConstraint("workspace_id", "branch", name="uq_env_workspace_branch"),
        Index("idx_env_workspace_status", "workspace_id", "status"),
        Index("idx_env_expiry", "status", "expires_at"),
    )


class EnvironmentEventModel(Base):
    """Append-only audit trail of environment lifecycle actions.

    Backs the UI timeline and compliance audit, and lets a partially failed
    orchestration (e.g. promote: merge succeeded, apply failed) be resumed from
    the step that failed.
    """

    __tablename__ = "environment_events"

    id = Column(String(64), primary_key=True)  # UUIDv4
    environment_id = Column(String(128), nullable=False)
    # create | open | plan | apply | test | run | promote | destroy | reconcile
    action = Column(String(16), nullable=False)
    actor_user_id = Column(String(64), nullable=False, default="")
    trigger_type = Column(String(16), nullable=False, default="human")  # human|agent|schedule
    commit_hash = Column(String(64), nullable=True)
    succeeded = Column(Integer, nullable=False, default=0)
    detail_json = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("idx_env_event_env", "environment_id", "created_at"),
    )
