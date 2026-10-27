"""Engine-side environment view — reconciliation and troubleshooting only.

This service reads the environments that ACTUALLY exist in the SQLMesh state.
It is deliberately read-only: the platform registry is the source of truth for
what an environment is, and destroying one is an orchestration
(:class:`~src.environments.registry.EnvironmentRegistry`) that must also release
the Git branch, the worktree and the Dagster deployment. Invalidating a virtual
environment on its own would leave exactly those orphans behind, which is why
the corresponding delete operation lives in the registry's SQLMesh binder.

the state read runs in the executor sandbox; this side only
caches and forwards the serialized view.
"""

from typing import Any, Dict, Optional

from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.logging import get_logger

logger = get_logger(__name__)


class EnvironmentService:
    """Read access to the environments present in the SQLMesh state."""

    async def list_environments(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the environments recorded in the workspace's SQLMesh state.

        Since the state schema is workspace-scoped, every member of the workspace
        sees the same list regardless of which working copy they queried from.
        Differences against the registry are drift for the reconciler to resolve.
        """
        try:
            return await get_metadata_context().environments(
                workspace_path, gateway=gateway
            )
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


# Singleton
_environment_service: Optional[EnvironmentService] = None


def get_environment_service() -> EnvironmentService:
    global _environment_service
    if _environment_service is None:
        _environment_service = EnvironmentService()
    return _environment_service
