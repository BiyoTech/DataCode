"""Tool: create_file — create or overwrite a file in the workspace.

The write is performed by workspace-runtime, the replica that holds this
working copy's lease: it is the single writer, and it creates the parent
directories. This service only decides WHAT to write.
"""

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools import workspace_fs
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

    try:
        await workspace_fs.write_text(workspace_path, file_path, content)
    except ValueError as exc:
        return f"Error: {exc}"
    except workspace_fs.WorkspaceFileError as exc:
        logger.warning("create_file_failed", path=file_path, error=str(exc))
        return f"Error: Failed to create file {file_path}: {exc}"

    logger.info("create_file_success", path=file_path, chars=len(content))
    return f"File created successfully: {file_path}"
