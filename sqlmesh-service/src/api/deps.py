"""Shared FastAPI dependencies for API route modules.

Provides service instances and common validation utilities.
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends

from src.config import settings
from src.core.command_service import get_command_service
from src.core.context_pool import get_context_pool
from src.core.environment_service import get_environment_service
from src.core.external_models_service import get_external_models_service
from src.core.lineage_service import get_lineage_service
from src.core.model_service import get_model_service
from src.core.plan_service import get_plan_service
from src.core.query_service import get_query_service
from src.core.schema_service import get_schema_service
from src.utils.errors import WorkspacePathInvalidError


def validate_workspace_path(workspace_path: str) -> str:
    """Validate workspace path exists and is within allowed base.

    Prevents path traversal attacks and ensures workspace exists.
    """
    base = Path(settings.workspace_base_path).expanduser().resolve()
    target = Path(workspace_path).expanduser().resolve()

    # Reject paths outside the allowed base (use relative_to, not string
    # startswith, so a sibling like /workspaces-evil is not accepted).
    try:
        target.relative_to(base)
    except ValueError:
        raise WorkspacePathInvalidError(
            workspace_path,
            reason=f"Path is outside allowed base: {base}",
        ) from None

    if not target.exists():
        raise WorkspacePathInvalidError(
            workspace_path,
            reason="Path does not exist",
        )

    return str(target)


# FastAPI dependency for query-parameter workspace_path validation.
WorkspacePath = Annotated[str, Depends(validate_workspace_path)]


__all__ = [
    "get_context_pool",
    "get_plan_service",
    "get_model_service",
    "get_lineage_service",
    "get_query_service",
    "get_schema_service",
    "get_command_service",
    "get_environment_service",
    "validate_workspace_path",
    "WorkspacePath",
]
