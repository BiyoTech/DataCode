"""Tool: query — execute ad-hoc SQL queries against the data warehouse.

Provides read-only SQL query execution for data analysis and exploration.
Queries are routed through workspace-runtime, which owns the working copy and
therefore its gateway configuration.
"""

import re
import typing as t

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.tools.runtime_client import RuntimeUnavailable, get_runtime_client
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Maximum characters for the formatted result to avoid context overflow
_MAX_RESULT_CHARS = 8000

# --- SQL read-only guard (defense-in-depth) ---
#
# The downstream SQLMesh Service is expected to enforce read-only execution,
# but we add a client-side check as a second layer. This catches the most
# common destructive patterns early, avoiding a round-trip and giving the LLM
# a clearer error message. This is NOT a full SQL parser.
#
# Pattern: forbidden keyword at a word boundary, case-insensitive. Word
# boundaries prevent false positives on column names like ``deleted_at``.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"DROP|DELETE|TRUNCATE|ALTER|CREATE|INSERT|UPDATE|GRANT|REVOKE|"
    r"MERGE|REPLACE|RENAME|ATTACH|DETACH|VACUUM|REINDEX"
    r")\b",
    re.IGNORECASE,
)


def _validate_readonly_sql(sql: str) -> t.Optional[str]:
    """Return an error message if *sql* is empty or contains a write keyword.

    Returns None when the query passes the read-only check.
    """
    if not sql or not sql.strip():
        return "SQL query cannot be empty."
    match = _FORBIDDEN_KEYWORDS.search(sql)
    if match:
        return (
            f"Rejected: keyword '{match.group(1)}' is not allowed in a "
            f"read-only query. Use the Warehouse Modeling Agent for DDL/DML "
            f"operations."
        )
    return None


@ToolRegistry.register("query", level=1)
@function_tool
async def query(ctx: RunContextWrapper[ToolContext], sql: str, limit: int = 100) -> str:
    """Execute an ad-hoc SQL query against the data warehouse (read-only).

    Use this tool for exploratory data analysis, validation queries, and
    ad-hoc investigation. This is NOT for creating/modifying models — use
    the Warehouse Modeling Agent for model work.

    Args:
        sql: The SQL query to execute (SELECT only, read-only).
        limit: Maximum number of rows to return (default 100). Prevents
               accidentally returning too much data.

    Returns:
        Query results as a markdown table, truncated if too large.
    """
    workspace_path = ctx.context.workspace_path

    # Defense-in-depth: reject destructive keywords before hitting the backend.
    validation_error = _validate_readonly_sql(sql)
    if validation_error:
        logger.warning(
    "query_rejected_readonly_violation",
    workspace_path=workspace_path, sql_len=len(sql),
)
        return validation_error

    logger.info("query_tool_called", workspace_path=workspace_path, limit=limit, sql_len=len(sql))

    try:
        data = await get_runtime_client().post(
            "/api/v1/query",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "sql": sql,
                "row_limit": limit,
            },
            timeout=60.0,
        )
    except RuntimeUnavailable as exc:
        # The client already resolved the lease, repaired a stale route and
        # waited out a materializing copy; reaching here means the query genuinely
        # could not run, so say so plainly rather than handing the LLM a status
        # code it cannot act on.
        logger.warning("query_failed", workspace_path=workspace_path, error=str(exc))
        return f"Query failed: {exc}"

    columns = data.get("columns", [])
    rows = data.get("rows", [])
    total_rows = data.get("total_rows", len(rows))
    execution_time_ms = data.get("execution_time_ms", 0)

    if not columns:
        return "Query executed successfully but returned no columns (empty result set)."

    # Build a markdown table for LLM readability
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, separator]

    for row in rows:
        # Each cell is already stringified by the service; join with markdown pipes
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")

    result = "\n".join(lines)

    # Truncate if the result is too large for the LLM context window
    if len(result) > _MAX_RESULT_CHARS:
        result = result[:_MAX_RESULT_CHARS] + "\n\n...(result truncated)"

    # Append metadata footer
    meta = f"\n\n*{total_rows} row(s) returned in {execution_time_ms} ms*"
    return result + meta
