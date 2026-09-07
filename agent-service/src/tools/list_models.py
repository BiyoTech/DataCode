"""Tool: list_models - list all models in the project.

Retrieves model metadata from workspace-runtime for project overview.
"""

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.tools.runtime_client import RuntimeUnavailable, get_runtime_client


@ToolRegistry.register("list_models", level=1)
@function_tool
async def list_models(ctx: RunContextWrapper[ToolContext], kind_filter: str = "") -> str:
    """List all data models in the current project.

    Shows model names, types (FULL/INCREMENTAL/VIEW), and descriptions.
    Use this to understand the current state of the project.

    Args:
        kind_filter: Optional filter by model kind
    (e.g., "FULL", "VIEW", "INCREMENTAL_BY_TIME_RANGE").

    Returns:
        A formatted list of models with their metadata.
    """
    workspace_path = ctx.context.workspace_path

    try:
        data = await get_runtime_client().get(
            "/api/v1/models",
            workspace_path=workspace_path,
            params={"workspace_path": workspace_path, "kind": kind_filter},
        )
    except RuntimeUnavailable as exc:
        return f"Error: could not list models: {exc}"

    models = data.get("models", [])
    if not models:
        return "No models found in the project."

    lines = ["## Project Models\n"]
    lines.append("| Model | Kind | Description |")
    lines.append("|-------|------|-------------|")
    for m in models:
        desc = m.get("description", "")[:50]
        lines.append(f"| {m['fqn']} | {m['kind']} | {desc} |")

    lines.append(f"\n**Total**: {len(models)} model(s)")
    return "\n".join(lines)
