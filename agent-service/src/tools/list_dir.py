"""Tool: list_dir — directory listing for the project workspace.

Lets agents browse the workspace structure without resorting to ``shell``
commands.  Supports optional glob filtering and recursive listing, with a
configurable entry cap to keep responses token-friendly.

The walk happens in workspace-runtime (which holds the files and the one
visibility policy every viewer shares); this tool caps and renders the result.
"""

import fnmatch

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools import workspace_fs
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_ENTRIES = 200


@ToolRegistry.register("list_dir", level=1)
@function_tool
async def list_dir(
    ctx: RunContextWrapper[ToolContext],
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> str:
    """List files and directories in the project workspace.

    Use this to explore the project structure, find files by name pattern, or
    discover what directories exist — without running shell commands.

    Directories are listed first (with a trailing ``/``), then files, both
    sorted alphabetically.

    Args:
        path: Directory to list, relative to the workspace root (default ``"."``).
        pattern: Glob pattern to filter entries (e.g. ``"*.py"``,
            ``"*.sql"``).  Default ``"*"`` lists everything.
        recursive: When ``True``, recurse into subdirectories.
        max_entries: Maximum number of entries to return (default 200).

    Returns:
        A formatted directory listing, or an error message.
    """
    workspace_path = ctx.context.workspace_path

    if max_entries < 1:
        max_entries = DEFAULT_MAX_ENTRIES

    try:
        entries = await workspace_fs.list_entries(
            workspace_path,
            path if path not in ("", ".") else "",
            # The name filter is applied here, on the FULL listing, because the
            # runtime matches a pattern against file names only: a directory
            # must stay visible under ``*.sql`` (it may contain matches), and a
            # recursive listing has to be filtered on the relative path.
            pattern="*",
            recursive=recursive,
            include_dirs=True,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    except workspace_fs.WorkspaceFileError as exc:
        logger.warning("list_dir_failed", path=path, error=str(exc))
        return f"Error: Failed to list {path}: {exc}"

    if not entries:
        # An empty answer covers both "nothing here" and "no such directory";
        # the runtime does not distinguish them, and neither changes what the
        # agent should do next.
        return _render(path, pattern, recursive, [], [], truncated=False)

    dirs: list[str] = []
    files: list[str] = []
    truncated = False
    for entry in entries:
        name = entry.path.rsplit("/", 1)[-1]
        if entry.is_directory:
            if pattern == "*" or fnmatch.fnmatch(name, pattern):
                dirs.append(entry.path + "/")
        elif pattern == "*" or fnmatch.fnmatch(name, pattern):
            files.append(entry.path)

        if len(dirs) + len(files) >= max_entries:
            truncated = True
            break

    dirs.sort()
    files.sort()

    logger.debug(
        "list_dir_served",
        path=path,
        pattern=pattern,
        recursive=recursive,
        dirs=len(dirs),
        files=len(files),
    )
    return _render(path, pattern, recursive, dirs, files, truncated=truncated)


def _render(
    path: str,
    pattern: str,
    recursive: bool,
    dirs: list[str],
    files: list[str],
    *,
    truncated: bool,
) -> str:
    lines = [f"## Directory: {path}"]
    mode_label = "recursive" if recursive else "flat"
    lines.append(
        f"Mode: {mode_label} | Pattern: `{pattern}` | "
        f"{len(dirs)} dir(s), {len(files)} file(s)"
    )

    if not dirs and not files:
        lines.append("\n(empty)")
        return "\n".join(lines)

    if dirs:
        lines.append("")
        lines.extend(f"  📁 {entry}" for entry in dirs)

    if files:
        lines.append("")
        lines.extend(f"  📄 {entry}" for entry in files)

    if truncated:
        lines.append(
            f"\n... (results capped at {len(dirs) + len(files)} entries. "
            f"Use a tighter pattern or non-recursive mode to narrow.)"
        )

    return "\n".join(lines)
