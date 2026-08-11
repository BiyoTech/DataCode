"""Tool: search_code — flexible code search across the project workspace.

Supports:
- Substring (default) or regex matching.
- Glob file patterns (defaults to all files, not just ``*.sql``).
- Context lines before/after each match.
- Three output modes: ``content``, ``files_with_matches``, ``count``.
- Configurable max results with graceful truncation.
"""

import fnmatch
import re
from pathlib import Path
from typing import List, Tuple

from agents import RunContextWrapper, function_tool

from src.context.tool_context import ToolContext
from src.tools._file_utils import EXCLUDE_DIRS, is_binary_file, resolve_safe_path
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

_VALID_OUTPUT_MODES = {"content", "files_with_matches", "count"}
DEFAULT_MAX_RESULTS = 50


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

    root = Path(workspace_path)
    if not root.exists():
        return f"Error: Workspace path does not exist: {workspace_path}"

    # Validate parameters.
    if output_mode not in _VALID_OUTPUT_MODES:
        return (
            f"Error: invalid output_mode '{output_mode}'. "
            f"Choose from: {', '.join(sorted(_VALID_OUTPUT_MODES))}."
        )
    if max_results < 1:
        max_results = DEFAULT_MAX_RESULTS
    if context_before < 0:
        context_before = 0
    if context_after < 0:
        context_after = 0

    # Compile matcher.
    try:
        if regex:
            pattern = re.compile(query, re.IGNORECASE)
            matcher = lambda line: bool(pattern.search(line))
        else:
            needle = query.lower()
            matcher = lambda line: needle in line.lower()
    except re.error as exc:
        return f"Error: invalid regex pattern: {exc}"

    # Collect results.
    # Each match: (rel_path, line_num, line_content, [context_lines])
    matches: List[Tuple[str, int, str, List[str]]] = []
    files_with_matches: dict[str, int] = {}

    for filepath in root.rglob("*"):
        # Skip excluded directories.
        if any(part in EXCLUDE_DIRS for part in filepath.parts):
            continue
        if not filepath.is_file():
            continue

        # Apply file_pattern (supports ``dir/**/*.py`` style patterns).
        rel = filepath.relative_to(root)
        rel_str = str(rel)
        if not _match_glob(rel_str, file_pattern):
            continue

        # Skip binary files.
        if is_binary_file(filepath):
            continue

        try:
            content = filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        file_lines = content.splitlines()
        file_match_count = 0

        for line_num, line in enumerate(file_lines, 1):
            if matcher(line):
                file_match_count += 1
                files_with_matches[rel_str] = (
                    files_with_matches.get(rel_str, 0) + 1
                )

                if output_mode == "content":
                    # Gather context lines.
                    ctx_start = max(0, line_num - 1 - context_before)
                    ctx_end = min(len(file_lines), line_num + context_after)
                    context_block = []
                    for ci in range(ctx_start, ctx_end):
                        cl = file_lines[ci]
                        marker = ">>" if (ci + 1) == line_num else "  "
                        context_block.append(f"  {marker} L{ci + 1}: {cl}")
                    matches.append((rel_str, line_num, line, context_block))

                    if len(matches) >= max_results:
                        break

        if output_mode == "content" and len(matches) >= max_results:
            break

    # Build output.
    if not files_with_matches:
        return f"No matches found for '{query}' in {file_pattern} files."

    if output_mode == "files_with_matches":
        return _format_files_with_matches(query, file_pattern, files_with_matches)

    if output_mode == "count":
        return _format_count(query, file_pattern, files_with_matches)

    # content mode
    return _format_content(
        query, file_pattern, matches, max_results
    )


# ─────────────────────── Output formatters ───────────────────────


def _match_glob(rel_path: str, pattern: str) -> bool:
    """Match *rel_path* against *pattern*, supporting ``**`` recursion."""
    if pattern == "*":
        return True
    # fnmatch doesn't handle ``**`` natively; do a two-pass check.
    # ``models/**/*.sql`` → match the tail ``*.sql`` and the head ``models/``.
    if "**/" in pattern:
        head, _, tail = pattern.partition("**/")
        # head is a directory prefix; tail is a file glob.
        if head and not rel_path.startswith(head):
            return False
        return fnmatch.fnmatch(rel_path.split("/")[-1], tail or "*")
    return fnmatch.fnmatch(rel_path, pattern)


def _format_content(
    query: str,
    file_pattern: str,
    matches: List[Tuple[str, int, str, List[str]]],
    max_results: int,
) -> str:
    lines = [f"## Search Results for '{query}'"]
    lines.append(
        f"Pattern: `{file_pattern}` | Found: {len(matches)} match(es)"
    )

    current_file = ""
    for rel_path, line_num, line_content, context_block in matches:
        if rel_path != current_file:
            current_file = rel_path
            lines.append(f"\n### {rel_path}")
        lines.append(f"  L{line_num}: {line_content[:200]}")
        for ctx_line in context_block:
            lines.append(ctx_line)

    if len(matches) >= max_results:
        lines.append(f"\n... (results capped at {max_results})")

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


def _format_count(
    query: str, file_pattern: str, files: dict[str, int]
) -> str:
    total = sum(files.values())
    lines = [
        f"## Match counts for '{query}'",
        f"Pattern: `{file_pattern}` | {len(files)} file(s), {total} total match(es)",
        "",
    ]
    for rel_path in sorted(files, key=lambda f: files[f], reverse=True):
        lines.append(f"  {files[rel_path]:>4}  {rel_path}")
    return "\n".join(lines)
