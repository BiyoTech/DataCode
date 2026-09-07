"""Tool: read_file — line-level file reader for the project workspace.

Supports ``offset`` / ``limit`` parameters so agents can read large files in
manageable windows (similar to Claude Code's ``read_file``).  Each response
includes a header showing the exact line range and total line count, so the
model knows whether more content remains.

Beyond plain text and code files, the tool also reads office documents
(PDF, Word, Excel, PowerPoint), parsing them to Markdown via the optional
``_office_parsers`` module before applying the same line-window mechanism.
"""

import asyncio
from typing import Optional

from agents import RunContextWrapper, function_tool
from src.context.tool_context import ToolContext
from src.tools._file_utils import (
    DEFAULT_READ_LINES,
    MAX_READ_LINES,
    is_binary_file,
    is_office_file,
    resolve_safe_path,
)
from src.tools._office_parsers import parse_office_document
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register("read_file", level=1)
@function_tool
async def read_file(
    ctx: RunContextWrapper[ToolContext],
    file_path: str,
    offset: int = 1,
    limit: int = DEFAULT_READ_LINES,
    sheet_name: Optional[str] = None,
    max_rows: int = 500,
    mode: str = "fast",
) -> str:
    """Read a portion of a file from the project workspace by line range.

    The file_path should be relative to the project root.  Use ``offset`` and
    ``limit`` to read large files in windows of up to 2000 lines at a time.

    The returned text begins with a header line of the form::

        [Lines 1-200 / 1523 total]

    so you always know how much content remains.  If the file has more lines
    after the requested window, read the next chunk by passing
    ``offset = previous_offset + lines_returned``.

    In addition to text and code files, this tool reads office documents
    (PDF, Word ``.docx``/``.doc``, Excel ``.xlsx``/``.xls``, PowerPoint
    ``.pptx``/``.ppt``).  Office documents are parsed to Markdown and then
    returned through the same line-window mechanism, so the agent can page
    through large documents just like source files.

    Args:
        file_path: Relative path within the workspace
            (e.g., ``"models/staging/stg_users.sql"`` or ``"docs/report.pdf"``).
        offset: 1-based starting line number (default 1).
        limit: Maximum number of lines to return (default 200, max 2000).
        sheet_name: Excel only.  Sheet name to read.  If omitted, all sheets
            are read and concatenated.
        max_rows: Excel only.  Maximum number of data rows per sheet
            (default 500).
        mode: Office document parsing mode.  ``"fast"`` (default) uses
            MarkItDown.  ``"detailed"`` is accepted for API symmetry but
            currently resolves to the same parser chain.

    Returns:
        The requested line range with a header, or an error message.
    """
    workspace_path = ctx.context.workspace_path

    # Validate and clamp parameters.
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = DEFAULT_READ_LINES
    if limit > MAX_READ_LINES:
        limit = MAX_READ_LINES

    try:
        full_path = resolve_safe_path(workspace_path, file_path)
    except ValueError as exc:
        return f"Error: {exc}"

    if not full_path.exists():
        return f"Error: File not found: {file_path}"
    if not full_path.is_file():
        return f"Error: Path is not a file: {file_path}"

    # Office documents (PDF/Word/Excel/PowerPoint) are binary by nature and
    # would be rejected by is_binary_file().  Route them to the dedicated
    # parser chain first; everything else falls through to the text path.
    if is_office_file(full_path):
        try:
            content, parser_name = await asyncio.to_thread(
                parse_office_document,
                str(full_path),
                sheet_name,
                max_rows,
                mode,
            )
        except Exception as exc:
            logger.warning("read_file_office_failed", path=file_path, error=str(exc))
            return f"Error reading office document {file_path}: {exc}"
    else:
        # Reject binary files early with a clear message.
        if is_binary_file(full_path):
            return (
                f"Error: File appears to be binary and cannot be read as text: {file_path}"
            )

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            parser_name = "text"
        except Exception as exc:
            logger.warning("read_file_io_failed", path=file_path, error=str(exc))
            return f"Error reading file {file_path}: {exc}"

    lines = content.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        return f"[Lines 0-0 / 0 total]\n\n## File: {file_path}\n\n(empty file)"

    # Convert 1-based offset to 0-based slice index.
    start_idx = offset - 1
    if start_idx >= total_lines:
        return (
            f"[Lines {offset}-{offset} / {total_lines} total]\n\n"
            f"Offset {offset} exceeds total lines ({total_lines}). "
            f"Use offset <= {total_lines}."
        )

    end_idx = min(start_idx + limit, total_lines)
    requested_lines = lines[start_idx:end_idx]
    has_more = end_idx < total_lines

    header = f"[Lines {offset}-{end_idx} / {total_lines} total]"
    if has_more:
        next_offset = end_idx + 1
        header += (
    f"  — {total_lines - end_idx} more line(s). "
    f"Call read_file with offset={next_offset} to continue."
)

    # Render with line numbers so the agent can reference exact positions.
    numbered = "\n".join(
        f"{idx:>6}: {line}"
        for idx, line in enumerate(requested_lines, start=offset)
    )

    logger.debug(
        "read_file_served",
        path=file_path,
        parser=parser_name,
        offset=offset,
        lines_returned=len(requested_lines),
        total_lines=total_lines,
    )

    return f"{header}\n\n## File: {file_path}\n\n```\n{numbered}\n```"
