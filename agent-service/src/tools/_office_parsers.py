"""Office document parsing utilities for the read_file tool.

Provides optional-dependency-aware parsers for PDF, Word, Excel and
PowerPoint files.  Each parser converts a document into Markdown text that
can be fed into the existing line-window rendering pipeline of ``read_file``.

Design notes
------------
- Dependencies are optional: the module imports cleanly even when no office
  parser is installed.  Availability is exposed via boolean flags and the
  :func:`get_available_parsers` helper.
- Every parser function owns its own ``try/except`` so that a failure in one
  parser can transparently trigger the next one in the fallback chain.
- The :func:`parse_office_document` dispatcher returns a
  ``(content, parser_name)`` tuple and raises ``RuntimeError`` with an
  actionable install hint when no parser is available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.utils.logging import get_logger

logger = get_logger(__name__)

# ──────────────────────── Optional dependency detection ────────────────────────
# Each flag is set at import time.  When the dependency is missing the
# corresponding parser raises a clear RuntimeError with install instructions.

_OPENPYXL_AVAILABLE = False
_MARKITDOWN_AVAILABLE = False
_PYTHON_DOCX_AVAILABLE = False
_PYTHON_PPTX_AVAILABLE = False

try:
    import openpyxl  # type: ignore

    _OPENPYXL_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    pass

try:
    from markitdown import MarkItDown  # type: ignore

    _MARKITDOWN_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    pass

try:
    from docx import Document as DocxDocument  # type: ignore

    _PYTHON_DOCX_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    pass

try:
    from pptx import Presentation  # type: ignore

    _PYTHON_PPTX_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    pass


# ──────────────────────── Supported extensions ────────────────────────
# Kept in sync with ``_file_utils.OFFICE_EXTENSIONS``; duplicated here so the
# parsers module is self-contained and can be unit-tested in isolation.

EXCEL_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xls", ".xlsm", ".xlsb"})
WORD_EXTENSIONS: frozenset[str] = frozenset({".docx", ".doc"})
PPTX_EXTENSIONS: frozenset[str] = frozenset({".pptx", ".ppt"})
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})


def _missing_deps_message(file_type: str, packages: list[str]) -> str:
    """Build a user-facing error message listing the packages to install."""
    install_cmd = "pip install " + " ".join(packages)
    return (
        f"Unable to read {file_type} file: required dependencies are missing. "
        f"Install them with: {install_cmd}"
    )


# ══════════════════════════════════════════════════════════════
# Individual parsers
# ══════════════════════════════════════════════════════════════


def read_excel(file_path: str, sheet_name: Optional[str] = None, max_rows: int = 500) -> str:
    """Read an Excel file with openpyxl and return Markdown table output.

    Args:
        file_path: Absolute path to the ``.xlsx`` / ``.xls`` / ``.xlsm`` file.
        sheet_name: Optional sheet name.  When omitted or unknown, all sheets
            are read and concatenated.
        max_rows: Maximum number of data rows (excluding header) per sheet.

    Returns:
        Markdown text with one ``## Sheet: <name>`` section per sheet.
    """
    if not _OPENPYXL_AVAILABLE:
        raise ImportError(_missing_deps_message("Excel", ["openpyxl", "markitdown"]))

    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        target_sheets = (
            [sheet_name]
            if sheet_name and sheet_name in workbook.sheetnames
            else workbook.sheetnames
        )

        parts: list[str] = []
        for current_sheet in target_sheets:
            worksheet = workbook[current_sheet]
            rows = list(worksheet.iter_rows(values_only=True))
            if not rows:
                parts.append(f"## Sheet: {current_sheet}\n\n(Empty sheet)")
                continue

            header = rows[0]
            column_names = [str(cell) if cell is not None else "" for cell in header]
            data_rows = rows[1 : max_rows + 1]

            lines = [f"## Sheet: {current_sheet}", ""]
            lines.append("| " + " | ".join(column_names) + " |")
            lines.append("| " + " | ".join(["---"] * len(column_names)) + " |")
            for row in data_rows:
                cells = [str(cell) if cell is not None else "" for cell in row]
                lines.append("| " + " | ".join(cells) + " |")

            if len(rows) - 1 > max_rows:
                lines.append("")
                lines.append(
                    f"> Showing only the first {max_rows} rows out of "
                    f"{len(rows) - 1} total data rows"
                )

            parts.append("\n".join(lines))
    finally:
        workbook.close()

    return "\n\n---\n\n".join(parts)


def read_with_markitdown(file_path: str) -> str:
    """Parse any supported document with MarkItDown and return Markdown text."""
    if not _MARKITDOWN_AVAILABLE:
        raise ImportError(_missing_deps_message("document", ["markitdown"]))

    markitdown = MarkItDown()
    result = markitdown.convert(file_path)
    return (result.text_content or "").strip()


def read_docx_fallback(file_path: str) -> str:
    """Read a ``.docx`` file via python-docx, preserving heading levels and tables."""
    if not _PYTHON_DOCX_AVAILABLE:
        raise ImportError(_missing_deps_message("Word", ["python-docx"]))

    document = DocxDocument(file_path)
    parts: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        # Map built-in "Heading N" styles to Markdown headings.
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            try:
                level = int(
                    paragraph.style.name.replace("Heading ", "").replace("Heading", "1")
                )
            except ValueError:
                level = 1
            parts.append(f"{'#' * level} {text}")
        else:
            parts.append(text)

    for index, table in enumerate(document.tables, 1):
        parts.append(f"\n### Table {index}\n")
        for row_index, row in enumerate(table.rows):
            cells = [cell.text.strip() for cell in row.cells]
            parts.append("| " + " | ".join(cells) + " |")
            if row_index == 0:
                parts.append("| " + " | ".join(["---"] * len(cells)) + " |")

    return "\n\n".join(parts).strip()


def read_pptx_fallback(file_path: str) -> str:
    """Read a ``.pptx`` file via python-pptx, extracting text and tables per slide."""
    if not _PYTHON_PPTX_AVAILABLE:
        raise ImportError(_missing_deps_message("PowerPoint", ["python-pptx"]))

    presentation = Presentation(file_path)
    parts: list[str] = [f"Slide count: {len(presentation.slides)}"]

    for index, slide in enumerate(presentation.slides, 1):
        slide_texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        slide_texts.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    slide_texts.append("| " + " | ".join(cells) + " |")

        parts.append(f"## Slide {index}")
        parts.append("\n".join(slide_texts) if slide_texts else "(Blank slide)")

    return "\n\n".join(parts).strip()


# ══════════════════════════════════════════════════════════════
# Dispatcher
# ══════════════════════════════════════════════════════════════


def parse_office_document(
    file_path: str,
    sheet_name: Optional[str] = None,
    max_rows: int = 500,
    mode: str = "fast",
) -> tuple[str, str]:
    """Parse an office document and return ``(markdown_content, parser_name)``.

    Fallback chain:
      - Excel  → openpyxl (precise sheet/row control) → markitdown
      - PDF    → markitdown
      - Word   → markitdown → python-docx (.docx only)
      - PPT    → markitdown → python-pptx (.pptx only)

    Args:
        file_path: Absolute path to the document.
        sheet_name: Excel only.  Sheet name to read; ``None`` reads all sheets.
        max_rows: Excel only.  Maximum data rows per sheet.
        mode: Reserved for future use.  ``"fast"`` and ``"detailed"`` currently
            both resolve to the same markitdown-first chain (docling is not
            bundled to keep the dependency footprint small).

    Returns:
        Tuple of ``(markdown_content, parser_name)``.

    Raises:
        RuntimeError: When no parser is available for the file type.
    """
    extension = Path(file_path).suffix.lower()
    _ = mode.strip().lower()  # accepted for API symmetry; no behavioral split yet

    # ── Excel: prefer openpyxl for precise sheet/row control ──────────────
    if extension in EXCEL_EXTENSIONS:
        if _OPENPYXL_AVAILABLE:
            try:
                content = read_excel(file_path, sheet_name, max_rows)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="openpyxl",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "openpyxl"
            except Exception as exc:  # noqa: BLE001 - fall through to next parser
                logger.warning("office_parser_failed", parser="openpyxl", error=str(exc))

        if _MARKITDOWN_AVAILABLE:
            try:
                content = read_with_markitdown(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="markitdown",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "markitdown"
            except Exception as exc:  # noqa: BLE001 - fall through to error
                logger.warning("office_parser_failed", parser="markitdown", error=str(exc))

        raise RuntimeError(_missing_deps_message("Excel", ["openpyxl", "markitdown"]))

    # ── PDF: markitdown only ──────────────────────────────────────────────
    if extension in PDF_EXTENSIONS:
        if _MARKITDOWN_AVAILABLE:
            try:
                content = read_with_markitdown(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="markitdown",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "markitdown"
            except Exception as exc:  # noqa: BLE001 - fall through to error
                logger.warning("office_parser_failed", parser="markitdown", error=str(exc))

        raise RuntimeError(_missing_deps_message("PDF", ["markitdown"]))

    # ── Word: markitdown → python-docx (.docx only) ───────────────────────
    if extension in WORD_EXTENSIONS:
        if _MARKITDOWN_AVAILABLE:
            try:
                content = read_with_markitdown(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="markitdown",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "markitdown"
            except Exception as exc:  # noqa: BLE001 - fall through to fallback
                logger.warning("office_parser_failed", parser="markitdown", error=str(exc))

        if _PYTHON_DOCX_AVAILABLE and extension == ".docx":
            try:
                content = read_docx_fallback(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="python-docx",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "python-docx"
            except Exception as exc:  # noqa: BLE001 - fall through to error
                logger.warning("office_parser_failed", parser="python-docx", error=str(exc))

        raise RuntimeError(_missing_deps_message("Word", ["markitdown", "python-docx"]))

    # ── PowerPoint: markitdown → python-pptx (.pptx only) ─────────────────
    if extension in PPTX_EXTENSIONS:
        if _MARKITDOWN_AVAILABLE:
            try:
                content = read_with_markitdown(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="markitdown",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "markitdown"
            except Exception as exc:  # noqa: BLE001 - fall through to fallback
                logger.warning("office_parser_failed", parser="markitdown", error=str(exc))

        if _PYTHON_PPTX_AVAILABLE and extension == ".pptx":
            try:
                content = read_pptx_fallback(file_path)
                if content:
                    logger.info(
                        "office_parsed",
                        parser="python-pptx",
                        ext=extension,
                        path=file_path,
                    )
                    return content, "python-pptx"
            except Exception as exc:  # noqa: BLE001 - fall through to error
                logger.warning("office_parser_failed", parser="python-pptx", error=str(exc))

        raise RuntimeError(
            _missing_deps_message("PowerPoint", ["markitdown", "python-pptx"])
        )

    # Should never happen: caller is expected to gate on is_office_file().
    raise RuntimeError(f"Unsupported office extension: {extension}")


def get_available_parsers() -> dict[str, bool]:
    """Return availability status for each optional parser.

    Useful for diagnostics and startup logs.
    """
    return {
        "markitdown": _MARKITDOWN_AVAILABLE,
        "openpyxl": _OPENPYXL_AVAILABLE,
        "python-docx": _PYTHON_DOCX_AVAILABLE,
        "python-pptx": _PYTHON_PPTX_AVAILABLE,
    }


__all__ = [
    "EXCEL_EXTENSIONS",
    "WORD_EXTENSIONS",
    "PPTX_EXTENSIONS",
    "PDF_EXTENSIONS",
    "parse_office_document",
    "get_available_parsers",
]
