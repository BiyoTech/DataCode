"""Model service - SQLMesh model operations.

P6 close-out: this is a thin caller of the executor sandbox. NOTHING here
touches a SQLMesh Context — listing, detail (with rendered SQL), metrics,
render and validation all execute where the user's Python models and macros
have already been exec'd: the sandbox. See :mod:`src.sqlmesh.metadata_context`
for the cache/read policy.
"""

from typing import Any, Dict, Optional

from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import (
    ErrorCode,
    service_error_from_exception,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ModelService:
    """Model read operations over the sandboxed executor."""

    async def list_models(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all models in the project.

        When ``environment`` names a promoted SQLMesh virtual environment
        (e.g. ``prod``/``dev``), the sandbox derives the listing from that
        environment's snapshots so it reflects what is actually deployed;
        otherwise the on-disk definition view is listed.

        Gateway handling: an explicit ``gateway`` lists only that gateway's
        models; no gateway aggregates every configured gateway (external
        models bound to any gateway become visible in one listing).
        """
        try:
            return await get_metadata_context().models(
                workspace_path, environment=environment, gateway=gateway
            )
        except Exception as e:
            logger.error("list_models_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list models",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.list",
                    gateway=gateway,
                ),
            ) from e

    async def get_model(
        self,
        workspace_path: str,
        model_name: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get detailed info for a single model including rendered SQL.

        Rendering executes the user's macros, which is why this is an
        execute-class sandbox op (not cached).
        """
        try:
            return await get_metadata_context().model_detail(
                workspace_path, model_name, gateway=gateway
            )
        except Exception as e:
            logger.error("get_model_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message=f"Failed to get model: {model_name}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.get",
                    gateway=gateway,
                    model_fqn=model_name,
                ),
            ) from e

    async def list_metrics(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all SQLMesh METRIC names defined in the project.

        Metric names are flat identifiers (not fully-qualified), e.g.
        ``total_orders``. Used by the ontology service to verify that a
        semantic-layer metric's ``maps_to`` points to a real SQLMesh METRIC.
        """
        try:
            return await get_metadata_context().metrics(
                workspace_path, gateway=gateway
            )
        except Exception as e:
            logger.error("list_metrics_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list metrics",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="metric.list",
                    gateway=gateway,
                ),
            ) from e

    async def render_model(
        self,
        workspace_path: str,
        model_name: str,
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        execution_time: Optional[str] = None,
        expand: bool = False,
        pretty: bool = True,
        dialect: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Render a model's SQL with macro expansion (sandbox op).

        Supports expand (expand referenced models), start/end/execution_time
        parameters, and dialect formatting — all applied inside the sandbox
        where the macros execute.
        """
        try:
            return await get_metadata_context().render(
                workspace_path,
                model_name,
                gateway=gateway,
                start=start,
                end=end,
                execution_time=execution_time,
                expand=expand,
                pretty=pretty,
                dialect=dialect,
            )
        except Exception as e:
            # Render failures are usually macro/SQL errors in the user's model
            # (a SQLMeshError). Surface the raw reason as the message so the CLI
            # and frontend show what to fix, matching the native `render` output.
            logger.error("render_model_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message=f"Failed to render model: {model_name}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.render",
                    gateway=gateway,
                    model_fqn=model_name,
                    context={
                        "start": start,
                        "end": end,
                        "expand": expand,
                        "dialect": dialect,
                    },
                ),
            ) from e

    async def validate(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate all model files by reloading the project in the sandbox.

        Always returns HTTP 200 with a structured verdict (this is the validate
        contract): ``valid`` plus an ``errors`` list. On failure we surface the
        full reason text so the frontend can render actionable diagnostics; an
        empty exception string falls back to the error code so the list is
        never a blank entry.
        """
        return await get_metadata_context().validate(workspace_path, gateway=gateway)


# Singleton
_model_service: Optional[ModelService] = None


def get_model_service() -> ModelService:
    global _model_service
    if _model_service is None:
        _model_service = ModelService()
    return _model_service
