"""Tool: get_lineage - get model dependency DAG (Lineage).

Retrieves the blood relationship graph showing how models depend on each other.
Calls the sqlmesh-service /lineage endpoint which returns:
  {
    "graph": {"<fqn>": ["<upstream_dep_fqn>", ...]},
    "nodes": {"<fqn>": {"name", "fqn", "type", "kind", "columns": [{name, type}]}}
  }
"""

import httpx
from agents import RunContextWrapper, function_tool

from src.config import settings
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register("get_lineage", level=1)
@function_tool
async def get_lineage(ctx: RunContextWrapper[ToolContext], model_name: str = "") -> str:
    """Get the model dependency lineage (DAG).

    Shows upstream and downstream dependencies for models.
    If a specific model is given, shows its focused subgraph (upstream + downstream).

    Args:
        model_name: Optional model name or FQN. If empty, returns full project lineage.

    Returns:
        A formatted description of model dependencies.
    """
    workspace_path = ctx.context.workspace_path
    logger.info("get_lineage_tool_invoked", workspace_path=workspace_path, model_name=model_name)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.sqlmesh_service_url}/api/v1/lineage",
                params={"workspace_path": workspace_path, "model_name": model_name},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        logger.error("get_lineage_timeout", workspace_path=workspace_path, model_name=model_name)
        return "Error: lineage request timed out. Please try again."
    except httpx.HTTPStatusError as e:
        logger.error(
            "get_lineage_http_error",
            status_code=e.response.status_code,
            workspace_path=workspace_path,
        )
        return f"Error: lineage service returned HTTP {e.response.status_code}."
    except Exception as e:
        logger.error("get_lineage_request_failed", error=str(e))
        return f"Error: failed to fetch lineage: {e}"

    graph = data.get("graph", {})
    nodes = data.get("nodes", {})

    if not graph:
        return "No lineage information available."

    lines = ["## Model Lineage\n"]

    for fqn, upstream_deps in graph.items():
        # Determine downstream: all nodes whose upstream list contains this fqn
        downstream = [other for other, deps in graph.items() if fqn in deps]

        node_info = nodes.get(fqn, {})
        kind = node_info.get("kind", "")
        model_type = node_info.get("type", "")
        columns = node_info.get("columns", [])
        col_count = len(columns)

        # Header line
        type_label = f" ({model_type}" + (f"/{kind}" if kind else "") + ")"
        col_label = f" [{col_count} columns]" if col_count else ""
        lines.append(f"### {fqn}{type_label}{col_label}")

        if upstream_deps:
            lines.append(f"  **Upstream** (depends on): {', '.join(upstream_deps)}")
        if downstream:
            lines.append(f"  **Downstream** (depended by): {', '.join(downstream)}")
        if not upstream_deps and not downstream:
            lines.append("  (No dependencies)")

        # Show column names if available
        if columns:
            col_names = [c.get("name", "?") for c in columns[:10]]
            suffix = f" ... (+{col_count - 10})" if col_count > 10 else ""
            lines.append(f"  **Columns**: {', '.join(col_names)}{suffix}")

        lines.append("")

    return "\n".join(lines)
