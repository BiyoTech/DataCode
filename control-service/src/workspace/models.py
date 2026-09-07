"""Database models for workspace management.

Defines ORM models for workspaces and user Git credentials.
All timestamps are stored in UTC.

Column definitions come from ``datacode_common.models``: the runtime maps the
same two tables, and a second hand-maintained copy of a schema drifts silently.
The declarative base stays LOCAL on purpose — this service's ``create_all``
covers everything in its metadata, so a shared base would make it start creating
the runtime's tables (see the shared module for the full argument).

P5 note: the apply-history and knowledge-document tables are owned by
workspace-runtime (the working-copy owner) — they are deliberately absent
from the control plane's metadata.
"""

from datacode_common.models import UserGitCredentialColumns, WorkspaceColumns
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for the CONTROL PLANE's tables only."""

    pass


class WorkspaceModel(WorkspaceColumns, Base):
    """Workspace metadata persisted in the database.

    Owned by this service: its migrate step is the only thing that creates this
    table. See ``datacode_common.models.workspace.WorkspaceColumns`` for the
    column contract and the reasoning behind it.
    """

    __tablename__ = "workspaces"


class UserGitCredentialModel(UserGitCredentialColumns, Base):
    """A user's Git provider credentials. Owned by this service."""

    __tablename__ = "user_git_credentials"
