"""ToolContext — runtime context injected into Agent tools via RunContextWrapper.

Carries session-level workspace information so tools don't need hardcoded paths.
Created per-request in main.py and passed to Runner.run_streamed(context=tool_context).

Usage in tool functions:
    from agents import RunContextWrapper
    from src.context.tool_context import ToolContext

    @function_tool
    def read_file(ctx: RunContextWrapper[ToolContext], file_path: str) -> str:
        workspace_path = ctx.context.workspace_path
        ...
"""

from dataclasses import dataclass, field


@dataclass
class ToolContext:
    """Runtime context for Agent tool execution.

    Provides workspace-scoped information so that tools can operate
    on the correct workspace without hardcoded paths.
    """

    workspace_path: str
    user_id: str
    session_id: str
    mode: str = "ask"
    execution_style: str = "agent"
    default_tool_level: int = 1
    require_confirmation_above: int = 3