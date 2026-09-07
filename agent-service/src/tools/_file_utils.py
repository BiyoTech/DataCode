"""Shared utilities for file-related agent tools.

Provides:
- ``resolve_safe_path``: resolve a relative path inside the workspace,
  rejecting path traversal attempts.
- ``is_hidden_entry``: the one place that decides what browsing and searching
  skip, delegated to the shared working-copy policy.
- ``is_binary_file``: quick heuristic to detect binary files that should
  not be read as text.
- ``MAX_READ_LINES``: hard ceiling for the ``limit`` parameter of read-based
  tools, aligned with Claude Code's 2000-line read window.
"""

import os
from pathlib import Path
from typing import Iterator

from datacode_common.workspace_layout import hidden_from_index

# Hard ceiling for line-based reads.  Agents may request up to this many lines
# in a single ``read_file`` call, matching the Claude Code read window so LLMs
# trained on that pattern can reuse their intuition.
MAX_READ_LINES = 2000

# Default number of lines returned when the caller does not specify a limit.
DEFAULT_READ_LINES = 200

# Office document extensions handled by the dedicated parser chain in
# ``_office_parsers``.  Files matching these extensions bypass the binary
# heuristic below (office formats legitimately contain NUL bytes) and are
# routed to the appropriate Markdown-emitting parser instead.
EXCEL_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xls", ".xlsm", ".xlsb"})
WORD_EXTENSIONS: frozenset[str] = frozenset({".docx", ".doc"})
PPTX_EXTENSIONS: frozenset[str] = frozenset({".pptx", ".ppt"})
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
OFFICE_EXTENSIONS: frozenset[str] = (
    EXCEL_EXTENSIONS | WORD_EXTENSIONS | PPTX_EXTENSIONS | PDF_EXTENSIONS
)


def resolve_safe_path(workspace_path: str, relative_path: str) -> Path:
    """Resolve *relative_path* inside *workspace_path*, rejecting traversal.

    Args:
        workspace_path: Absolute path to the workspace root.
        relative_path: Path provided by the agent (may be relative or
            absolute, but must resolve *inside* the workspace).

    Returns:
        The resolved absolute ``Path``.

    Raises:
        ValueError: If the resolved path escapes the workspace root.
    """
    root = Path(workspace_path).resolve()
    candidate = Path(relative_path)
    target = candidate if candidate.is_absolute() else (root / candidate)
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Access denied: path is outside workspace: {relative_path}"
        ) from None
    return target


def is_hidden_entry(root: Path, entry: Path) -> bool:
    """Whether *entry* must be skipped when browsing or searching the copy.

    The policy itself lives in ``datacode_common.workspace_layout``, shared with
    the runtime that serves the user's file explorer: one working copy must not
    look different depending on who is looking at it. What matters HERE is the
    argument it is given.

    The path is made workspace-RELATIVE first, and that is the whole point of
    this wrapper. Matching directory names against an absolute path also matches
    the components above the workspace, and the platform's own default workspace
    root is ``~/.datacode/workspaces/<tenant>/...`` — an absolute match on the
    platform state directory would therefore hide every file of every workspace.

    Note what this is NOT: a permission boundary. ``read_file``, ``create_file``
    and ``apply_patch`` take an explicit path and do not consult it; hiding an
    entry keeps it out of listings and search results, nothing more.
    """
    try:
        relative = entry.relative_to(root)
    except ValueError:
        # Outside the copy: resolve_safe_path is what refuses those. Anything
        # that gets here is not part of the tree being browsed.
        return True
    return hidden_from_index(relative.as_posix())


def iter_visible_entries(
    root: Path, target: Path, *, recursive: bool
) -> Iterator[Path]:
    """Yield the entries under *target* that browsing and search may see.

    Recursion PRUNES rather than filters: ``rglob`` used to enumerate every
    file of every virtual environment and dependency tree before discarding
    them one by one, which on a real working copy is most of the walk. Skipping
    a directory here skips its whole subtree.

    Symlinked directories are not followed (``os.walk`` default): a link back
    into an ancestor would otherwise loop, and a link out of the copy would
    list files the caller never asked about.

    Args:
        root: Workspace root; entries are judged relative to it.
        target: Directory to iterate. May be *root* itself or below it.
        recursive: Whether to descend.
    """
    if not recursive:
        for entry in sorted(target.iterdir()):
            if not is_hidden_entry(root, entry):
                yield entry
        return

    for current, dirnames, filenames in os.walk(target):
        current_path = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not is_hidden_entry(root, current_path / name)
        )
        for name in dirnames:
            yield current_path / name
        for name in sorted(filenames):
            entry = current_path / name
            if not is_hidden_entry(root, entry):
                yield entry


def is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    """Heuristic: return ``True`` if *path* looks like a binary file.

    Reads at most *sample_size* bytes and checks for NUL bytes or a high
    proportion of non-text bytes, similar to how ``git`` decides a file is
    binary.
    """
    try:
        with path.open("rb") as fh:
            chunk = fh.read(sample_size)
    except OSError:
        return True  # If we cannot even read it, treat as binary to be safe.

    if b"\x00" in chunk:
        return True

    # Decode as UTF-8 (errors ignored) and check the ratio of printable chars.
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True

    if not text:
        return False

    # Count control characters (excluding common whitespace).
    allowed_ctrl = {"\n", "\r", "\t"}
    non_text = sum(1 for ch in text if ch < " " and ch not in allowed_ctrl)
    return non_text / len(text) > 0.30


def is_office_file(path: Path) -> bool:
    """Return ``True`` when *path* points to a supported office document.

    Office files (PDF/Word/Excel/PowerPoint) are binary by nature and would
    be rejected by :func:`is_binary_file`.  Callers should check this helper
    *before* the binary check so that office documents are routed to the
    dedicated parser chain in ``_office_parsers`` instead of being rejected.
    """
    return path.suffix.lower() in OFFICE_EXTENSIONS
