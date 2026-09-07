"""Tool: schema_discovery - query data source table/column schemas.

Calls workspace-runtime to discover available tables and their columns from the
project's configured Gateway (database connection).
"""

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.tools.runtime_client import RuntimeUnavailable, get_runtime_client


@ToolRegistry.register("schema_discovery", level=1)
@function_tool
async def schema_discovery(
    ctx: RunContextWrapper[ToolContext],
    schema_name: str,
    table_name: str = "",
) -> str:
    """Discover table and column schemas from the data warehouse.

    Use this tool to understand what data sources are available for building models.

    Args:
        schema_name: The database schema to explore (e.g., "public", "raw").
        table_name: Optional specific table name. If empty, lists all tables in the schema.

    Returns:
        A formatted string describing the available tables and their columns.
    """
    workspace_path = ctx.context.workspace_path

    try:
        data = await get_runtime_client().post(
            "/api/v1/schema/discover",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "schema_name": schema_name,
                "table": table_name,
            },
        )
    except RuntimeUnavailable as exc:
        return f"Error: schema discovery failed: {exc}"

    tables = data.get("tables", [])
    if not tables:
        return f"No tables found in schema '{schema_name}'."

    # Format output for the LLM
    lines = [f"## Schema: {schema_name}\n"]
    for table in tables:
        lines.append(f"### Table: {table['table_name']}")
        lines.append("| Column | Type | Nullable |")
        lines.append("|--------|------|----------|")
        for col in table.get("columns", []):
            nullable = "YES" if col.get("nullable", True) else "NO"
            lines.append(f"| {col['name']} | {col['data_type']} | {nullable} |")
        lines.append("")

    return "\n".join(lines)
