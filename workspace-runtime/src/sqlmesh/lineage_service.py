"""Lineage service - model and column dependency DAG operations.

a thin caller of the executor sandbox. Model-level lineage is
served from the runtime's cached metadata (ms-level); column-level lineage
renders the traced column's expression — which executes the user's macros —
so it is an execute-class sandbox op. The graph builders and column-lineage
walkers live in the sandbox (``src/executor/metadata_ops.py``), next to the
Context they need.
"""

from typing import Any, Dict, Optional

from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.logging import get_logger

logger = get_logger(__name__)


class LineageService:
    """Lineage read operations over the sandboxed executor."""

    async def get_lineage(
        self,
        workspace_path: str,
        model_name: str = "",
        gateway: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get model dependency DAG.

        If model_name is provided, the sandbox returns a focused subgraph
        containing both upstream ancestors and downstream descendants of the
        given model, along with per-node metadata (type, kind, columns).
        Otherwise the full project DAG is returned. When ``environment`` names
        a promoted SQLMesh virtual environment, the topology is derived from
        that environment's snapshots instead of the local definition.
        """
        try:
            return await get_metadata_context().lineage(
                workspace_path,
                model_name=model_name,
                gateway=gateway,
                environment=environment,
            )
        except Exception as e:
            logger.error("get_lineage_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to get lineage",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="lineage.get",
                    gateway=gateway,
                    model_fqn=model_name or None,
                ),
            ) from e

    async def get_column_lineage(
        self,
        workspace_path: str,
        model_name: str,
        column_name: str,
        models_only: bool = False,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get column-level lineage as adjacency list.

        Matches SQLMesh web server's GET /lineage/{model}/{column} behavior.
        """
        try:
            return await get_metadata_context().column_lineage(
                workspace_path,
                model_name,
                column_name,
                models_only=models_only,
                gateway=gateway,
            )
        except Exception as e:
            logger.error("get_column_lineage_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to get column lineage",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="lineage.get_column",
                    gateway=gateway,
                    model_fqn=model_name,
                    context={"column_name": column_name},
                ),
            ) from e


# Singleton
_lineage_service: Optional[LineageService] = None


def get_lineage_service() -> LineageService:
    global _lineage_service
    if _lineage_service is None:
        _lineage_service = LineageService()
    return _lineage_service
