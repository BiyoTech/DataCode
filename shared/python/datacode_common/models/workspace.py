"""Shared column definitions for the tables two services both read.

``workspaces`` and ``user_git_credentials`` are owned by the control plane, which
creates them; the runtime reads them through its own ORM classes. Both sides had a
full, byte-identical copy of the column definitions, which is a schema waiting to
drift — and a drifted column is not a test failure, it is data silently truncated
or a query that stops matching.

Why MIXINS rather than one shared declarative base
--------------------------------------------------
A shared ``Base`` would put every service's tables into one ``MetaData``, and that
breaks the platform's "one creator per table" rule in two concrete ways:

* the control plane calls ``Base.metadata.create_all()`` with no argument, so it
  would start creating the runtime's tables — the exact second-creator race the
  rule exists to prevent (concurrent ``CREATE TABLE IF NOT EXISTS`` can fail
  inside PostgreSQL's catalog);
* alembic's ``target_metadata`` would suddenly contain the runtime's tables, so
  autogenerate would propose migrations for tables the control plane does not own.

Declarative mixins avoid both: SQLAlchemy COPIES the columns into each mapped
subclass, so the definitions are shared while each service's ``MetaData`` still
contains exactly the tables that service declares. Ownership stays where it is;
only the duplication goes away.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import declared_attr

__all__ = [
    "utc_now",
    "WorkspaceColumns",
    "UserGitCredentialColumns",
]


def utc_now() -> datetime:
    """Current UTC datetime (timezone-aware).

    Every timestamp in the platform is stored in UTC; the frontend converts to
    the user's zone for display.
    """
    return datetime.now(timezone.utc)


class WorkspaceColumns:
    """Columns of the ``workspaces`` table.

    A workspace is a tenant-level shared unit. Git is the only durable carrier of
    its code, therefore NO local filesystem path is stored: each user's working
    copy path is derived at runtime (see ``datacode_common.workspace_paths``).

    Branches are NOT a workspace attribute: a branch belongs to an environment.
    The workspace only names which environment is its production one and which is
    opened by default.

    Membership and roles are NOT stored here: authorization is centralized in the
    gateway, which owns the role bindings and tells each service what the caller
    may do.
    """

    id = Column(String(128), primary_key=True)  # ws-{tenant_id}-{slug}
    tenant_id = Column(String(64), nullable=False)
    owner_user_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    dialect = Column(String(32), nullable=False, default="duckdb")
    git_repo_url = Column(String(512), nullable=True)
    # Production environment name (Git ``main``); every workspace has exactly one.
    production_env = Column(String(40), nullable=False, default="prod")
    # Environment a member lands on when opening the workspace.
    default_env = Column(String(40), nullable=False, default="prod")
    status = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    last_active_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        # Built per subclass: an Index object belongs to one Table, so a shared
        # instance would attach to whichever class was defined first.
        return (Index("idx_workspaces_tenant_status", "tenant_id", "status"),)


class UserGitCredentialColumns:
    """Columns of the ``user_git_credentials`` table.

    A user may hold several credentials: one OAuth link per provider plus any
    number of personal access tokens. Which one a remote operation uses is decided
    by host matching (see ``datacode_common.git_providers``), not by this table.
    """

    id = Column(String(64), primary_key=True)  # UUID
    user_id = Column(String(64), nullable=False)
    provider = Column(String(32), nullable=False)  # github / gitlab / bitbucket / gitea
    nickname = Column(String(128), nullable=False, default="")
    auth_type = Column(String(16), nullable=False, default="pat")  # oauth / pat
    token = Column(Text, nullable=False)  # Encrypted token
    email = Column(String(256), nullable=True)
    username = Column(String(128), nullable=True)
    server_url = Column(String(512), nullable=True)  # Git server URL (self-hosted)
    is_default = Column(Integer, nullable=False, default=0)  # 1=default, 0=not
    oauth_account = Column(String(256), nullable=True)  # Display name for OAuth
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            Index("idx_git_cred_user_provider", "user_id", "provider"),
            Index("idx_git_cred_user_default", "user_id", "is_default"),
        )
