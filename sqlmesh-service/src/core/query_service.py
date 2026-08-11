"""Query service - safe ad-hoc SQL execution.

Provides query execution with:
- Safe LIMIT injection via sqlglot (no string concatenation)
- Configurable max row limit
- Execution time measurement
"""

import asyncio
import time
from typing import Any, Dict, Optional

from src.config import settings
from src.core.context_pool import get_context_pool
from src.core.format import format_query_result
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


def _apply_safe_limit(sql: str, limit: int) -> str:
    """Safely apply LIMIT to SQL using sqlglot parser.

    Validates limit is a positive integer and caps at max configured value.
    Falls back to string append if sqlglot fails to parse.
    """
    if not isinstance(limit, int) or limit <= 0:
        limit = settings.query_default_row_limit

    limit = min(limit, settings.query_max_row_limit)

    try:
        from sqlglot import parse_one
        from sqlglot.expressions import Limit

        expr = parse_one(sql)
        # Only add LIMIT if not already present
        if not expr.find(Limit):
            expr = expr.limit(limit)
        return expr.sql()
    except Exception:
        # Fallback: simple append (still safe since limit is validated int)
        clean_sql = sql.rstrip().rstrip(";")
        if "LIMIT" not in clean_sql.upper().split("\n")[-1]:
            return f"{clean_sql} LIMIT {limit}"
        return clean_sql


class QueryService:
    """Ad-hoc SQL query execution via SQLMesh engine adapter."""

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
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        if row_limit <= 0:
            row_limit = settings.query_default_row_limit

        # Apply safe LIMIT
        safe_sql = _apply_safe_limit(sql, row_limit)

        try:
            start_time = time.time()
            df = await asyncio.to_thread(entry.context.fetchdf, safe_sql)
            execution_time_ms = int((time.time() - start_time) * 1000)

            columns = list(df.columns)
            rows = [
                [str(v) if v is not None else "" for v in row]
                for _, row in df.iterrows()
            ]

            logger.info(
                "query_executed",
                workspace_path=workspace_path,
                execution_time_ms=execution_time_ms,
                row_count=len(rows),
            )
            return format_query_result(columns, rows, execution_time_ms)

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
                    sql=safe_sql,
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
