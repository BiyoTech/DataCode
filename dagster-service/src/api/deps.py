"""Shared FastAPI dependencies for API route modules.

Provides workspace path validation (path-traversal safe) and singleton
accessors for the core managers.
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends

from src.config import settings
from src.core.code_location_manager import get_code_location_manager
from src.core.dagster_client import get_dagster_client
from src.core.run_manager import get_run_manager
from src.utils.errors import WorkspacePathInvalidError


def validate_workspace_path(workspace_path: str) -> str:
    """Validate workspace path exists and is within the allowed base.

    Mirrors sqlmesh-service: rejects paths outside the configured base (using
    ``relative_to`` rather than string prefix) to prevent path traversal, and
    ensures the workspace directory exists on disk.
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

    return str(target)


# FastAPI dependency for query-parameter workspace_path validation.
WorkspacePath = Annotated[str, Depends(validate_workspace_path)]


__all__ = [
    "validate_workspace_path",
    "WorkspacePath",
    "get_dagster_client",
    "get_run_manager",
    "get_code_location_manager",
]
