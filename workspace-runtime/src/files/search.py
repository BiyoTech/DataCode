"""Content search across a working copy — the engine behind the agent's ``search_code``.

Lives here, next to the files, because this service owns them: agent-service
walked the tree itself until P3d, which only worked where the copy happened to
be on the same machine. What crosses the wire now is a QUERY and a structured
result; the presentation (the markdown the model reads) stays with the tool.

Two properties this shares with the rest of ``src.files`` on purpose:

* the same visibility policy (``datacode_common.workspace_layout``) — one
  working copy must not look different depending on who is looking at it;
* pruning rather than filtering — a dependency tree is skipped whole, not
  enumerated and discarded file by file.
"""

from __future__ import annotations

import fnmatch
import os
import re
import typing as t
from dataclasses import dataclass, field

from datacode_common.workspace_layout import hidden_from_index

from src.files.ops import looks_binary
from src.utils.logging import get_logger

logger = get_logger(__name__)

# One matched line is reported at most this long: a minified file would
# otherwise spend a whole response on one line.
MAX_LINE_CHARS = 500

# Read cap per file. Above this a file is a data blob, not source to grep.
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class SearchMatch:
    """One matching line, with the context lines the caller asked for."""

    path: str
    line: int
    text: str
    context: t.List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchOutcome:
    """Everything the three output modes need, from one walk.

    ``files`` is always populated (it is a counter, effectively free) so the
    "files with matches" and "count" modes need no second pass; ``matches`` is
    only collected when the caller wants the lines themselves.
    """

    matches: t.List[SearchMatch]
    files: t.Dict[str, int]
    truncated: bool


def search_workspace(
    workspace_path: str,
    query: str,
    *,
    file_pattern: str = "*",
    regex: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    max_results: int = 50,
    include_matches: bool = True,
) -> SearchOutcome:
    """Search file contents under *workspace_path*.

    Args:
        query: Substring (case-insensitive) or, with ``regex``, a pattern.
        file_pattern: Glob over the workspace-RELATIVE path, ``**`` supported.
        include_matches: When false only per-file counts are collected, which
            is what the counting output modes need.

    Raises:
        re.error: The caller supplied an invalid regular expression. Surfaced
            rather than swallowed: it is a usage error with a fixable message.
    """
    matcher = _build_matcher(query, regex=regex)

    matches: list[SearchMatch] = []
    files: dict[str, int] = {}
    truncated = False

    for rel_path, lines in _iter_text_files(workspace_path, file_pattern):
        for line_no, line in enumerate(lines, start=1):
            if not matcher(line):
                continue
            files[rel_path] = files.get(rel_path, 0) + 1
            if not include_matches:
                continue
            matches.append(
                SearchMatch(
                    path=rel_path,
                    line=line_no,
                    text=line[:MAX_LINE_CHARS],
                    context=_context_block(
                        lines, line_no, context_before, context_after
                    ),
                )
            )
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    logger.debug(
        "workspace_search_completed",
        files_with_matches=len(files),
        matches=len(matches),
        truncated=truncated,
    )
    return SearchOutcome(matches=matches, files=files, truncated=truncated)


def _build_matcher(query: str, *, regex: bool) -> t.Callable[[str], bool]:
    if regex:
        compiled = re.compile(query, re.IGNORECASE)
        return lambda line: bool(compiled.search(line))
    needle = query.lower()
    return lambda line: needle in line.lower()


def _iter_text_files(
    workspace_path: str, file_pattern: str
) -> t.Iterator[t.Tuple[str, t.List[str]]]:
    """Yield ``(relative_path, lines)`` for every searchable file."""
    for root, dirs, filenames in os.walk(workspace_path):
        dirs[:] = sorted(
            name
            for name in dirs
            if not hidden_from_index(
                os.path.relpath(os.path.join(root, name), workspace_path)
            )
        )
        for name in sorted(filenames):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, workspace_path)
            if hidden_from_index(rel):
                continue
            if not match_glob(rel, file_pattern):
                continue
            try:
                with open(full, "rb") as handle:
                    data = handle.read(MAX_FILE_BYTES)
            except OSError:
                continue
            if looks_binary(data[:8192]):
                continue
            yield rel, data.decode("utf-8", errors="ignore").splitlines()


def _context_block(
    lines: t.Sequence[str], line_no: int, before: int, after: int
) -> t.List[str]:
    """Render the context window around a 1-based match line."""
    if before <= 0 and after <= 0:
        return []
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no + after)
    return [
        f"  {'>>' if index + 1 == line_no else '  '} L{index + 1}: "
        f"{lines[index][:MAX_LINE_CHARS]}"
        for index in range(start, end)
    ]


def match_glob(relative_path: str, pattern: str) -> bool:
    """Match a workspace-relative path against a glob, ``**`` included.

    ``fnmatch`` has no notion of ``**``, so ``models/**/*.sql`` is split into a
    directory prefix and a file glob — the shape callers actually write.
    """
    if not pattern or pattern == "*":
        return True
    if "**/" in pattern:
        head, _, tail = pattern.partition("**/")
        if head and not relative_path.startswith(head):
            return False
        return fnmatch.fnmatch(relative_path.split("/")[-1], tail or "*")
    return fnmatch.fnmatch(relative_path, pattern)
