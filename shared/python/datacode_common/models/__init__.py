"""Shared ORM column definitions.

Only tables that TWO services map are here. Each service keeps its own
declarative base and therefore its own ``MetaData``, so "who creates a table"
is unchanged — see ``workspace.py`` for why mixins rather than a shared base.
"""

from datacode_common.models.workspace import (
    UserGitCredentialColumns,
    WorkspaceColumns,
    utc_now,
)

__all__ = [
    "UserGitCredentialColumns",
    "WorkspaceColumns",
    "utc_now",
]
