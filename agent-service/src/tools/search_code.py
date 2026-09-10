"""Tool: search_code — flexible code search across the project workspace.

Supports:
- Substring (default) or regex matching.
- Glob file patterns (defaults to all files, not just ``*.sql``).
- Context lines before/after each match.
- Three output modes: ``content``, ``files_with_matches``, ``count``.
- Configurable max results with graceful truncation.

The grep runs in workspace-runtime (``src/files/search.py``) — it has the files.
What is left here is the part the model consumes: parameter validation and the
three renderings.
"""

from typing import List

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools import workspace_fs
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

_VALID_OUTPUT_MODES = {"content", "files_with_matches", "count"}
DEFAULT_MAX_RESULTS = 50

# The runtime bounds these too; clamping here keeps the model's mistake from
# becoming a rejected request it cannot interpret.
MAX_CONTEXT_LINES = 20


@ToolRegistry.register("search_code", level=1)
@function_tool
async def search_code(
    ctx: RunContextWrapper[ToolContext],
    query: str,
    file_pattern: str = "*",
    regex: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    max_results: int = DEFAULT_MAX_RESULTS,
    output_mode: str = "content",
) -> str:
    """Search for code content or filenames in the project workspace.

    Use this to find models, configurations, or any code that matches a pattern.

    Args:
        query: Text to search for. Case-insensitive substring match by default;
            set ``regex=True`` to interpret as a regular expression.
        file_pattern: Glob pattern for files to search (default ``"*"`` = all
            files). Examples: ``"*.py"``, ``"*.sql"``, ``"models/**/*.sql"``.
        regex: When ``True``, treat *query* as a Python regex.
        context_before: Number of context lines to show before each match.
        context_after: Number of context lines to show after each match.
        max_results: Maximum number of matching lines to return
            (default 50). Ignored when ``output_mode`` is ``files_with_matches``
            or ``count``.
        output_mode: Controls the result format:
            - ``"content"`` — show matching lines with file/line info (default).
            - ``"files_with_matches"`` — list only file paths that contain
              matches (saves tokens).
            - ``"count"`` — show match counts per file.

    Returns:
        A formatted string of search results.
    """
    workspace_path = ctx.context.workspace_path

    if output_mode not in _VALID_OUTPUT_MODES:
        return (
            f"Error: invalid output_mode '{output_mode}'. "
            f"Choose from: {', '.join(sorted(_VALID_OUTPUT_MODES))}."
        )
    if not (query or "").strip():
        return "Error: query is required."
    if max_results < 1:
        max_results = DEFAULT_MAX_RESULTS
    context_before = min(max(context_before, 0), MAX_CONTEXT_LINES)
    context_after = min(max(context_after, 0), MAX_CONTEXT_LINES)

    wants_lines = output_mode == "content"
    try:
        result = await workspace_fs.search(
            workspace_path,
            query,
            file_pattern=file_pattern or "*",
            regex=regex,
            context_before=context_before if wants_lines else 0,
            context_after=context_after if wants_lines else 0,
            max_results=max_results,
            include_matches=wants_lines,
        )
    except workspace_fs.WorkspaceFileError as exc:
        logger.warning("search_code_failed", query=query, error=str(exc))
        return f"Error: search failed: {exc}"

    if not result.files:
        return f"No matches found for '{query}' in {file_pattern} files."

    if output_mode == "files_with_matches":
        return _format_files_with_matches(query, file_pattern, result.files)
    if output_mode == "count":
        return _format_count(query, file_pattern, result.files)
    return _format_content(query, file_pattern, result.hits, result.truncated)


# ─────────────────────── Output formatters ───────────────────────


def _format_content(
    query: str,
    file_pattern: str,
    hits: List[workspace_fs.SearchHit],
    truncated: bool,
) -> str:
    lines = [f"## Search Results for '{query}'"]
    lines.append(f"Pattern: `{file_pattern}` | Found: {len(hits)} match(es)")

    current_file = ""
    for hit in hits:
        if hit.path != current_file:
            current_file = hit.path
            lines.append(f"\n### {hit.path}")
        lines.append(f"  L{hit.line}: {hit.text[:200]}")
        lines.extend(hit.context)

    if truncated:
        lines.append(f"\n... (results capped at {len(hits)})")

    return "\n".join(lines)


def _format_files_with_matches(
    query: str, file_pattern: str, files: dict[str, int]
) -> str:
    lines = [
        f"## Files matching '{query}'",
        f"Pattern: `{file_pattern}` | {len(files)} file(s)",
        "",
    ]
    for rel_path in sorted(files):
        lines.append(f"  {rel_path}  ({files[rel_path]} match(es))")
    return "\n".join(lines)


def _format_count(query: str, file_pattern: str, files: dict[str, int]) -> str:
    total = sum(files.values())
    lines = [
        f"## Match counts for '{query}'",
        f"Pattern: `{file_pattern}` | {len(files)} file(s), {total} total match(es)",
        "",
    ]
    for rel_path in sorted(files, key=lambda f: files[f], reverse=True):
        lines.append(f"  {files[rel_path]:>4}  {rel_path}")
    return "\n".join(lines)
