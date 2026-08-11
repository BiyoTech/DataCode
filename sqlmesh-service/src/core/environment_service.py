"""Environment service - SQLMesh environment operations.

Matches SQLMesh web server's environments endpoint behavior.
"""

import asyncio
from typing import Any, Dict, Optional

from src.core.context_pool import get_context_pool
from src.core.format import format_environments
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class EnvironmentService:
    """Environment read/delete operations using SQLMesh SDK."""

    async def list_environments(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get all environments.

        Matches SQLMesh web server's GET /environments behavior:
        - Lists all environments from state reader
        - Includes pinned and default target environment info
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            return await asyncio.to_thread(format_environments, entry.context)
        except Exception as e:
            logger.error("list_environments_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list environments",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="environment.list",
                    gateway=gateway,
                ),
            ) from e

    async def delete_environment(
        self,
        workspace_path: str,
        environment: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Invalidate and delete an environment.

        Matches SQLMesh web server's DELETE /environments/{environment} behavior.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        try:
            await asyncio.to_thread(entry.context.state_sync.invalidate_environment, environment)
            await asyncio.to_thread(entry.context.state_sync.delete_expired_environments)

            logger.info("environment_deleted", workspace_path=workspace_path, environment=environment)
            return {"deleted": True, "environment": environment}
        except Exception as e:
            logger.error("delete_environment_failed", error=str(e), environment=environment)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message=f"Failed to delete environment: {environment}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="environment.delete",
                    gateway=gateway,
                    context={"environment": environment},
                ),
            ) from e


# Singleton
_environment_service: Optional[EnvironmentService] = None


def get_environment_service() -> EnvironmentService:
    global _environment_service
    if _environment_service is None:
        _environment_service = EnvironmentService()
    return _environment_service
