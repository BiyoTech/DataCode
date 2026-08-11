"""Tool: create_file - create or modify files in the workspace.

Writes generated model SQL or configuration files to the user's workspace
via the Workspace Manager or directly to the filesystem (MVP).
"""

from agents import RunContextWrapper, function_tool

from src.context.tool_context import ToolContext
from src.tools._file_utils import resolve_safe_path
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register("create_file", level=2)
@function_tool
async def create_file(ctx: RunContextWrapper[ToolContext], file_path: str, content: str) -> str:
    """Create or overwrite a file in the project workspace.

    Use this to write SQLMesh model SQL files, configuration files, or test files.
    The file_path should be relative to the project root (e.g., "models/staging/stg_users.sql").

    Args:
        file_path: Relative path within the workspace (e.g., "models/analytics/user_activity.sql").
        content: The full file content to write.

    Returns:
        Confirmation message with the file path.
    """
    workspace_path = ctx.context.workspace_path

    # Security check: resolve and verify the path stays inside the workspace.
    try:
        full_path = resolve_safe_path(workspace_path, file_path)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        logger.info("create_file_success", path=file_path, chars=len(content))
        return f"File created successfully: {file_path}"
    except OSError as exc:
        logger.warning("create_file_failed", path=file_path, error=str(exc))
        return f"Error: Failed to create file {file_path}: {exc}"
