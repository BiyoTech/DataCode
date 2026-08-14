"""File operations within a workspace.

Provides safe file CRUD with path traversal protection.
All paths are validated to stay within workspace boundaries.
"""

import os
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Directories that are never listed. Dot-entries in general ARE listed (an
# editor's explorer shows ``.gitignore``, ``.env``, ``.cache`` ...), but Git's
# object store holds thousands of machine-written files and carries no user
# value, so it stays hidden everywhere.
_EXCLUDED_DIRS = frozenset({".git"})

# Additionally pruned when walking recursively (flat file index behind search
# and @-mentions): regenerable, machine-written trees that would otherwise
# drown the real project files. They remain browsable in the lazy tree.
_RECURSION_EXCLUDED_DIRS = _EXCLUDED_DIRS | frozenset({".cache", ".dagster"})


@dataclass
class FileInfo:
    """File metadata."""
    path: str             # Relative to workspace root
    is_directory: bool
    size_bytes: int
    modified_at: str      # UTC ISO 8601


def read_file(workspace_path: str, relative_path: str) -> t.Optional[str]:
    """Read file content from workspace.

    Args:
        workspace_path: Absolute workspace root path.
        relative_path: Path relative to workspace root.

    Returns:
        File content string, or None if not found.
    """
    full_path = _safe_path(workspace_path, relative_path)
    if not full_path or not os.path.isfile(full_path):
        return None
    with open(full_path, "r") as f:
        return f.read()


def write_file(workspace_path: str, relative_path: str, content: str) -> str:
    """Write content to a file (create or overwrite).

    Auto-creates parent directories.

    Args:
        workspace_path: Absolute workspace root path.
        relative_path: Path relative to workspace root.
        content: File content to write.

    Returns:
        Absolute path of written file.
    """
    full_path = _safe_path(workspace_path, relative_path)
    if not full_path:
        raise ValueError(f"Invalid path: {relative_path}")

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)

    logger.info("file_written", workspace=workspace_path, path=relative_path)
    return full_path


def delete_file(workspace_path: str, relative_path: str) -> bool:
    """Delete a file from workspace.

    Returns:
        True if deleted, False if not found.
    """
    full_path = _safe_path(workspace_path, relative_path)
    if not full_path or not os.path.exists(full_path):
        return False
    os.remove(full_path)
    logger.info("file_deleted", workspace=workspace_path, path=relative_path)
    return True


def list_files(
    workspace_path: str,
    directory: str = "",
    recursive: bool = False,
    pattern: str = "",
) -> t.List[FileInfo]:
    """List files in a workspace directory.

    Hidden (dot-prefixed) entries ARE returned so files such as ``.gitignore``
    are visible and editable; only :data:`_EXCLUDED_DIRS` (and, when recursing,
    :data:`_RECURSION_EXCLUDED_DIRS`) are skipped.

    Args:
        workspace_path: Absolute workspace root path.
        directory: Subdirectory to list (relative), empty = root.
        recursive: Whether to recurse into subdirectories.
        pattern: Glob pattern filter (e.g. "*.sql").

    Returns:
        List of FileInfo objects.
    """
    base = _safe_path(workspace_path, directory) if directory else workspace_path
    if not base or not os.path.isdir(base):
        return []

    files: t.List[FileInfo] = []

    if recursive:
        for root, dirs, filenames in os.walk(base):
            # Prune in place so os.walk never descends into excluded trees.
            dirs[:] = [d for d in dirs if d not in _RECURSION_EXCLUDED_DIRS]
            for name in filenames:
                if pattern and not _matches_pattern(name, pattern):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, workspace_path)
                stat = os.stat(full)
                files.append(FileInfo(
                    path=rel,
                    is_directory=False,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                ))
    else:
        for entry in os.scandir(base):
            if entry.is_dir() and entry.name in _EXCLUDED_DIRS:
                continue
            if pattern and not entry.is_dir() and not _matches_pattern(entry.name, pattern):
                continue
            rel = os.path.relpath(entry.path, workspace_path)
            stat = entry.stat()
            files.append(FileInfo(
                path=rel,
                is_directory=entry.is_dir(),
                size_bytes=stat.st_size if not entry.is_dir() else 0,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            ))

    return sorted(files, key=lambda f: (not f.is_directory, f.path))


def create_directory(workspace_path: str, relative_path: str) -> str:
    """Create a directory within workspace.

    Args:
        workspace_path: Absolute workspace root path.
        relative_path: Directory path relative to workspace root.

    Returns:
        Absolute path of created directory.
    """
    full_path = _safe_path(workspace_path, relative_path)
    if not full_path:
        raise ValueError(f"Invalid path: {relative_path}")

    os.makedirs(full_path, exist_ok=True)
    logger.info("directory_created", workspace=workspace_path, path=relative_path)
    return full_path


def rename_file(workspace_path: str, old_relative: str, new_relative: str) -> str:
    """Rename or move a file/directory within workspace.

    Args:
        workspace_path: Absolute workspace root path.
        old_relative: Current path relative to workspace root.
        new_relative: New path relative to workspace root.

    Returns:
        Absolute path of the renamed file/directory.
    """
    old_full = _safe_path(workspace_path, old_relative)
    new_full = _safe_path(workspace_path, new_relative)

    if not old_full or not new_full:
        raise ValueError(f"Invalid path: {old_relative} -> {new_relative}")

    if not os.path.exists(old_full):
        raise FileNotFoundError(f"Source not found: {old_relative}")

    # Ensure parent directory of target exists
    os.makedirs(os.path.dirname(new_full), exist_ok=True)
    os.rename(old_full, new_full)

    logger.info("file_renamed", workspace=workspace_path, old=old_relative, new=new_relative)
    return new_full


def _safe_path(workspace_path: str, relative_path: str) -> t.Optional[str]:
    """Validate and construct a safe absolute path (prevents traversal attacks)."""
    clean = os.path.normpath(relative_path)
    if clean.startswith("..") or os.path.isabs(clean):
        logger.warning("path_traversal_blocked", path=relative_path)
        return None

    full = os.path.join(workspace_path, clean)
    # Double-check resolved path stays within workspace
    if not os.path.realpath(full).startswith(os.path.realpath(workspace_path)):
        logger.warning("path_escape_blocked", path=relative_path)
        return None

    return full


def _matches_pattern(filename: str, pattern: str) -> bool:
    """Simple glob pattern match (supports *.ext)."""
    import fnmatch
    return fnmatch.fnmatch(filename, pattern)
