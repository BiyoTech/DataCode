"""Shared FastAPI dependencies for API route modules.

Provides workspace path validation (path-traversal safe) and singleton
accessors for the core managers.
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends

from datacode_common.logging.context import bind_workspace_context

from src.config import settings
from src.core.dagster_client import get_dagster_client
from src.core.deployment_manager import get_deployment_manager
from src.core.run_manager import get_run_manager
from src.utils.errors import WorkspacePathInvalidError


def validate_workspace_path(workspace_path: str) -> str:
    """Validate that a path is an existing working copy inside the allowed base.

    Mirrors sqlmesh-service: rejects paths outside the configured base (using
    ``relative_to`` rather than a string prefix) to prevent path traversal, and
    ensures the directory exists on disk. The layout itself (five segments ending
    in ``envs/{environment}``) is enforced where the deployment identity is
    derived, so the error names the environment rather than the raw path.

    Also binds the working copy to the request's logging context, which is what
    routes this service's log lines into that workspace's ``logs/`` directory.
    """
    base = Path(settings.workspace_base_path).expanduser().resolve()
    target = Path(workspace_path).expanduser().resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise WorkspacePathInvalidError(
            workspace_path, reason=f"Path is outside allowed base: {base}"
        ) from None

    if not target.exists():
        raise WorkspacePathInvalidError(workspace_path, reason="Path does not exist")

    resolved = str(target)
    bind_workspace_context(resolved)
    return resolved


# FastAPI dependency for query-parameter workspace_path validation.
WorkspacePath = Annotated[str, Depends(validate_workspace_path)]


__all__ = [
    "validate_workspace_path",
    "WorkspacePath",
    "get_dagster_client",
    "get_run_manager",
    "get_deployment_manager",
]
