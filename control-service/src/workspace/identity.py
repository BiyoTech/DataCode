"""Platform identity behind an environment working-copy path.

Most requests carry a single handle — ``workspace_path`` — while the platform
needs the tuple it encodes: the tenant (isolation), the workspace (the shared
SQLMesh state), the user (the working copy) and the environment (the SQLMesh
virtual environment and the Dagster deployment).

The layout convention itself lives in :mod:`datacode_common.workspace_paths`
and stays pure. This module is the thin, settings-aware adapter on top of it,
so no caller has to know the configured base path or re-implement the
derivation.

NOTE (P5): the control plane derives identities to ADDRESS the runtime's
env-control API; it never inspects the directory the path points at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from datacode_common import workspace_paths as ws_paths

from src.config import settings
from src.utils.errors import WorkspacePathInvalidError


@dataclass(frozen=True)
class WorkspaceIdentity:
    """The (tenant, workspace, user, environment) tuple of one working copy."""

    tenant_id: str
    workspace_id: str
    user_id: str
    environment: str

    @property
    def environment_id(self) -> str:
        """Registry key of the environment this working copy is checked out to."""
        from datacode_common.naming import environment_id

        return environment_id(self.workspace_id, self.environment)


def workspace_base_path() -> str:
    """Return the configured root of every workspace working copy."""
    return os.path.expanduser(settings.workspace_base_path)


def resolve_workspace_identity(workspace_path: str) -> WorkspaceIdentity:
    """Decompose a working-copy path into its platform identity.

    Raises:
        WorkspacePathInvalidError: the path is outside the base directory or does
            not match the ``{base}/{tenant}/{workspace}/{user}/envs/{env}``
            layout. Rejecting instead of guessing is what keeps a caller from
            reaching another tenant's directory.
    """
    try:
        tenant_id, workspace_id, user_id, environment = ws_paths.parse_workspace_path(
            workspace_base_path(), workspace_path
        )
    except ws_paths.WorkspacePathLayoutError as exc:
        raise WorkspacePathInvalidError(workspace_path, reason=str(exc)) from exc

    return WorkspaceIdentity(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        environment=environment,
    )
