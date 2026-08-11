"""Artifact helpers for file-writing tools."""

FILE_WRITING_TOOLS = {"create_file"}

_EXT_LANGUAGE_MAP = {
    ".sql": "sql",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".ts": "typescript",
    ".js": "javascript",
}


def guess_language(file_path: str) -> str:
    """Infer a syntax-highlight language hint from a file path extension."""
    for ext, lang in _EXT_LANGUAGE_MAP.items():
        if file_path.endswith(ext):
            return lang
    return "text"
