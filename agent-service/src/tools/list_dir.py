"""Tool: list_dir — directory listing for the project workspace.

Lets agents browse the workspace structure without resorting to ``shell``
commands.  Supports optional glob filtering and recursive listing, with a
configurable entry cap to keep responses token-friendly.
"""

import fnmatch
from pathlib import Path

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools._file_utils import iter_visible_entries, resolve_safe_path
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
        target = resolve_safe_path(workspace_path, path)
    except ValueError as exc:
        return f"Error: {exc}"

    if not target.exists():
        return f"Error: Path not found: {path}"
    if not target.is_dir():
        return f"Error: Path is not a directory: {path}"

    root = Path(workspace_path).resolve()

    # Collect entries.
    dirs: list[str] = []
    files: list[str] = []

    truncated = False
    count = 0
    # Machine-written trees (caches, virtual environments, the platform's own
    # state) are pruned by the iterator, so they cost nothing to skip.
    for entry in iter_visible_entries(root, target, recursive=recursive):
        if not entry.exists():
            continue

        rel = entry.relative_to(root)
        rel_str = str(rel)

        # For recursive listings, apply the glob to the file name.
        name = entry.name
        if entry.is_dir():
            # Always include directories when pattern is "*", even in recursive
            # mode, so the agent can see the structure.
            if pattern == "*" or fnmatch.fnmatch(name, pattern):
                dirs.append(rel_str + "/")
        elif entry.is_file():
            if pattern == "*" or fnmatch.fnmatch(name, pattern):
                files.append(rel_str)

        count += 1
        if (len(dirs) + len(files)) >= max_entries:
            truncated = True
            break

    dirs.sort()
    files.sort()

    # Build output.
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
        for d in dirs:
            lines.append(f"  📁 {d}")

    if files:
        lines.append("")
        for f in files:
            lines.append(f"  📄 {f}")

    if truncated:
        lines.append(
            f"\n... (results capped at {max_entries} entries. "
            f"Use a tighter pattern or non-recursive mode to narrow.)"
        )

    logger.debug(
        "list_dir_served",
        path=path,
        pattern=pattern,
        recursive=recursive,
        dirs=len(dirs),
        files=len(files),
    )

    return "\n".join(lines)
