"""Shared utilities for file-related agent tools.

Provides:
- ``resolve_safe_path``: resolve a relative path inside the workspace,
  rejecting path traversal attempts.
- ``is_binary_file``: quick heuristic to detect binary files that should
  not be read as text.
- ``MAX_READ_LINES``: hard ceiling for the ``limit`` parameter of read-based
  tools, aligned with Claude Code's 2000-line read window.
"""

from pathlib import Path

# Hard ceiling for line-based reads.  Agents may request up to this many lines
# in a single ``read_file`` call, matching the Claude Code read window so LLMs
# trained on that pattern can reuse their intuition.
MAX_READ_LINES = 2000

# Default number of lines returned when the caller does not specify a limit.
DEFAULT_READ_LINES = 200

# Directories that are never useful for code search / listing.
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".cache",
        "__pycache__",
        "node_modules",
        ".agent",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

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
