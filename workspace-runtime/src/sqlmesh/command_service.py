"""Command service - evaluate, test, table_diff operations.

all three execute user code (model rendering, test files,
macro expansion in diffs) and therefore run in the executor sandbox. This
module only translates the sandbox's pure-data results into the endpoint
contracts the REST layer has always served.
"""

from typing import Any, Dict, Optional

from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.logging import get_logger

logger = get_logger(__name__)


class CommandService:
    """SQLMesh command operations over the sandboxed executor."""

    async def evaluate(
        self,
        workspace_path: str,
        model: str,
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        execution_time: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Evaluate a model and return results.

        Matches SQLMesh web server's POST /commands/evaluate behavior. External
        models (schema-only stubs) are queried directly against their physical
        table — handled inside the sandbox, where the model definitions live.
        """
        try:
            result = await get_metadata_context().evaluate(
                workspace_path,
                model,
                gateway=gateway,
                start=start,
                end=end,
                execution_time=execution_time,
                limit=limit,
            )
            logger.info(
                "model_evaluated",
                model=model,
                row_count=result.get("total_rows"),
                execution_time_ms=result.get("execution_time_ms"),
            )
            return result
        except Exception as e:
            # Evaluation errors are typically SQL/macro problems in the model
            # (a SQLMeshError). Surface the raw reason so clients see the exact
            # failing SQL/expression rather than a generic wrapper.
            logger.error("evaluate_failed", error=str(e), model=model)
            raise service_error_from_exception(
                e,
                code=ErrorCode.QUERY_FAILED,
                fallback_message=f"Failed to evaluate model: {model}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.evaluate",
                    gateway=gateway,
                    model_fqn=model,
                    context={"start": start, "end": end, "limit": limit},
                ),
            ) from e

    async def test(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        *,
        test: Optional[str] = None,
        verbosity: str = "default",
    ) -> Dict[str, Any]:
        """Run model tests.

        Matches SQLMesh web server's GET /commands/test behavior. The test
        files are the user's own code and execute in the sandbox; the rich
        per-test result (with tracebacks) crosses the wire as pure data.
        """
        try:
            result = await get_metadata_context().test(
                workspace_path,
                gateway=gateway,
                test=test,
                verbosity=verbosity,
            )
            logger.info(
                "tests_completed",
                workspace_path=workspace_path,
                tests_run=result.get("tests_run"),
            )
            return result
        except Exception as e:
            logger.error("test_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to run tests",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.test",
                    gateway=gateway,
                    context={"test": test, "verbosity": verbosity},
                ),
            ) from e

    async def table_diff(
        self,
        workspace_path: str,
        source: str,
        target: str,
        gateway: Optional[str] = None,
        *,
        on: Optional[str] = None,
        model_or_snapshot: Optional[str] = None,
        where: Optional[str] = None,
        temp_schema: Optional[str] = None,
        limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """Calculate differences between tables.

        Matches SQLMesh web server's GET /table_diff behavior. Rendering the
        compared models executes the user's macros — hence the sandbox.
        """
        try:
            return await get_metadata_context().table_diff(
                workspace_path,
                source,
                target,
                gateway=gateway,
                on=on,
                model_or_snapshot=model_or_snapshot,
                where=where,
                temp_schema=temp_schema,
                limit=limit,
            )
        except Exception as e:
            logger.error("table_diff_failed", error=str(e), source=source, target=target)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to compute table diff",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.table_diff",
                    gateway=gateway,
                    context={
                        "source": source,
                        "target": target,
                        "model_or_snapshot": model_or_snapshot,
                    },
                ),
            ) from e


# Singleton
_command_service: Optional[CommandService] = None


def get_command_service() -> CommandService:
    global _command_service
    if _command_service is None:
        _command_service = CommandService()
    return _command_service
