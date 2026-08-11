"""Tool: schema_discovery - query data source table/column schemas.

Calls SQLMesh Service to discover available tables and their columns
from the project's configured Gateway (database connection).
"""

import httpx
from agents import RunContextWrapper, function_tool

from src.config import settings
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry


@ToolRegistry.register("schema_discovery", level=1)
@function_tool
async def schema_discovery(ctx: RunContextWrapper[ToolContext], schema_name: str, table_name: str = "") -> str:
    """Discover table and column schemas from the data warehouse.

    Use this tool to understand what data sources are available for building models.

    Args:
        schema_name: The database schema to explore (e.g., "public", "raw").
        table_name: Optional specific table name. If empty, lists all tables in the schema.

    Returns:
        A formatted string describing the available tables and their columns.
    """
    workspace_path = ctx.context.workspace_path

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.sqlmesh_service_url}/api/v1/schema/discover",
            json={
                "workspace_path": workspace_path,
                "schema_name": schema_name,
                "table": table_name,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()

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
