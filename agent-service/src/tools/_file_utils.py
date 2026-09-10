"""Shared, PURELY LEXICAL helpers for the file tools.

What used to live here — path resolution against the real filesystem, the
visibility policy for a tree walk, the binary heuristic — moved to
workspace-runtime (``src/files/ops.py`` and ``src/files/search.py``). All three
need the files themselves, and this service has none: it reaches a working copy
only through :mod:`src.tools.workspace_fs`.

What is left is what a caller can decide without touching a disk: how big a read
window may be, and which extensions need the office parser chain instead of a
text read.
"""

# Hard ceiling for line-based reads.  Agents may request up to this many lines
# in a single ``read_file`` call, matching the Claude Code read window so LLMs
# trained on that pattern can reuse their intuition.
MAX_READ_LINES = 2000

# Default number of lines returned when the caller does not specify a limit.
DEFAULT_READ_LINES = 200

# Office document extensions handled by the dedicated parser chain in
# ``_office_parsers``.  Files matching these extensions are fetched as BYTES and
# parsed to Markdown; everything else is fetched as text.
EXCEL_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xls", ".xlsm", ".xlsb"})
WORD_EXTENSIONS: frozenset[str] = frozenset({".docx", ".doc"})
PPTX_EXTENSIONS: frozenset[str] = frozenset({".pptx", ".ppt"})
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
OFFICE_EXTENSIONS: frozenset[str] = (
    EXCEL_EXTENSIONS | WORD_EXTENSIONS | PPTX_EXTENSIONS | PDF_EXTENSIONS
)


def file_extension(file_path: str) -> str:
    """The lower-cased extension of *file_path*, including the dot."""
    name = file_path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name[1:]:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def is_office_file(file_path: str) -> bool:
    """Whether *file_path* names a supported office document.

    Extension-based, and it has to be: the decision is made BEFORE the bytes
    are fetched, because the two cases fetch differently (text vs base64).
    """
    return file_extension(file_path) in OFFICE_EXTENSIONS
