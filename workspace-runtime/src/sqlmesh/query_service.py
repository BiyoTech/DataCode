"""Query service - safe ad-hoc SQL execution.

P6 close-out: execution runs in the executor sandbox. The LIMIT is applied
with sqlglot INSIDE the sandbox (no string concatenation), bounded by the
platform's max row limit, and the sandbox's warehouse connection is the
working copy's own gateway — never a platform credential.
"""

from typing import Any, Dict, Optional

from src.config import settings
from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.logging import get_logger

logger = get_logger(__name__)


class QueryService:
    """Ad-hoc SQL query execution via the sandboxed executor."""

    async def execute(
        self,
        workspace_path: str,
        sql: str,
        gateway: Optional[str] = None,
        row_limit: int = 0,
    ) -> Dict[str, Any]:
        """Execute a SQL query and return results.

        Args:
            workspace_path: Target workspace path.
            sql: SQL query string.
            gateway: Optional gateway override.
            row_limit: Maximum rows to return. 0 = use default.
        """
        if row_limit <= 0:
            row_limit = settings.query_default_row_limit

        try:
            result = await get_metadata_context().query(
                workspace_path, sql, gateway=gateway, row_limit=row_limit
            )
            logger.info(
                "query_executed",
                workspace_path=workspace_path,
                execution_time_ms=result.get("execution_time_ms"),
                row_count=result.get("total_rows"),
            )
            return result
        except Exception as e:
            logger.error("query_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.QUERY_FAILED,
                fallback_message="Query execution failed",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="query.execute",
                    gateway=gateway,
                    sql=sql,
                    context={"row_limit": row_limit},
                ),
            ) from e


# Singleton
_query_service: Optional[QueryService] = None


def get_query_service() -> QueryService:
    global _query_service
    if _query_service is None:
        _query_service = QueryService()
    return _query_service
