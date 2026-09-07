"""File operations within a workspace.

Provides safe file CRUD with path traversal protection.
All paths are validated to stay within workspace boundaries.
"""

import os
import shutil
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone

from datacode_common.workspace_layout import hidden_from_index, hidden_from_tree

from src.utils.errors import InvalidRequestError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Which entries the explorer and the flat index may show is decided by
# ``datacode_common.workspace_layout``, shared with the agent's own file tools:
# one working copy must not look different depending on who is looking at it.
#
# Two tiers, and the difference matters here:
#
# * :func:`hidden_from_tree` — never listed, not even in the lazy tree. Dot
#   entries in general ARE listed (an explorer shows ``.gitignore``, ``.env``,
#   ``.cache`` ...); this tier is only for trees where a user decision is
#   meaningless — Git's object store, byte-code, the platform's own state.
# * :func:`hidden_from_index` — additionally pruned when walking recursively
#   (the flat index behind search and @-mentions), where a virtual environment
#   or a cache would drown the real project files. Those remain browsable.

# Names whose removal destroys the working copy itself rather than something in
# it: ``.git`` is the copy's only durable carrier. The explorer never offers
# them, but the agent tool surface (``/rt/v1/tools/delete``) reaches the delete
# path directly, so the guard lives with the implementation.
_PROTECTED_NAMES = frozenset({".git"})


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


def delete_path(workspace_path: str, relative_path: str) -> bool:
    """Delete a file, symlink or directory from the workspace.

    The ENTRY'S KIND decides the syscall. ``unlink(2)`` cannot remove a
    directory, and each platform says so differently — ``EISDIR`` on Linux,
    ``EPERM`` ("Operation not permitted") on macOS/BSD — so the previous
    unconditional ``os.remove`` turned every "delete this folder" click into an
    opaque 500 that reads like a filesystem permissions problem.

    A directory is removed together with everything under it: that is the one
    delete semantic the explorer exposes (the UI confirms with the user first),
    and the agent tool surface inherits it under the same path guards.

    Returns:
        True if deleted, False if not found.

    Raises:
        InvalidRequestError: the path escapes the workspace, or names the
            workspace root or its Git metadata.
    """
    full_path = _safe_delete_path(workspace_path, relative_path)
    if not full_path:
        # A blocked traversal is a bad request, not a missing file.
        raise InvalidRequestError(f"Invalid path: {relative_path}")

    # lexists, not exists: a broken symlink must stay deletable.
    if not os.path.lexists(full_path):
        return False

    _assert_deletable(workspace_path, full_path, relative_path)

    if os.path.islink(full_path):
        # Never follow the link: its target may be a directory, or live outside
        # the workspace entirely.
        os.unlink(full_path)
        deleted = "symlink"
    elif os.path.isdir(full_path):
        # rmtree unlinks the symlinks it meets rather than descending into them.
        shutil.rmtree(full_path)
        deleted = "directory"
    else:
        os.remove(full_path)
        deleted = "file"

    logger.info(
        "path_deleted",
        workspace=workspace_path,
        path=relative_path,
        kind=deleted,
    )
    return True


def _assert_deletable(workspace_path: str, full_path: str, relative_path: str) -> None:
    """Refuse the two deletions that break the working copy instead of editing it.

    The workspace root IS the copy, and ``.git`` is the only durable carrier of
    its content — both are structural, not a matter of permissions, and neither
    is recoverable from inside the product.
    """
    if os.path.realpath(full_path) == os.path.realpath(workspace_path):
        raise InvalidRequestError("The workspace root cannot be deleted")

    parts = os.path.normpath(relative_path).split(os.sep)
    if any(part in _PROTECTED_NAMES for part in parts):
        raise InvalidRequestError(
            f"'{relative_path}' is Git metadata of the working copy; it cannot be deleted"
        )


def list_files(
    workspace_path: str,
    directory: str = "",
    recursive: bool = False,
    pattern: str = "",
) -> t.List[FileInfo]:
    """List files in a workspace directory.

    Hidden (dot-prefixed) entries ARE returned so files such as ``.gitignore``
    are visible and editable; only the machine-written trees named by
    ``datacode_common.workspace_layout`` are skipped — the never-listed tier
    when listing a single directory, the wider index tier when recursing.

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
            # Prune in place so os.walk never descends into excluded trees:
            # enumerating a virtual environment only to discard it costs the
            # walk everything it would have cost to keep.
            dirs[:] = [
                d
                for d in dirs
                if not hidden_from_index(
                    os.path.relpath(os.path.join(root, d), workspace_path)
                )
            ]
            for name in filenames:
                if pattern and not _matches_pattern(name, pattern):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, workspace_path)
                if hidden_from_index(rel):
                    continue
                stat = os.stat(full)
                files.append(FileInfo(
                    path=rel,
                    is_directory=False,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                ))
    else:
        for entry in os.scandir(base):
            rel = os.path.relpath(entry.path, workspace_path)
            if hidden_from_tree(rel):
                continue
            if pattern and not entry.is_dir() and not _matches_pattern(entry.name, pattern):
                continue
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
    """Validate and construct a safe absolute path (prevents traversal attacks).

    The entry's OWN resolved path must sit inside the workspace: everything
    here goes on to read or write through that path, so a symlink pointing out
    of the copy is an escape, not a shortcut.
    """
    clean = _normalized_relative(workspace_path, relative_path)
    if clean is None:
        return None

    full = os.path.join(workspace_path, clean)
    if not _is_inside(os.path.realpath(workspace_path), os.path.realpath(full)):
        logger.warning("path_escape_blocked", path=relative_path)
        return None

    return full


def _safe_delete_path(workspace_path: str, relative_path: str) -> t.Optional[str]:
    """Containment for a DELETE: the entry itself need not resolve inside it.

    Unlinking a symlink is safe wherever it points — it removes the NAME, never
    the target — so validating the entry's own resolved path (as
    :func:`_safe_path` does) would make every link to the outside undeletable
    and leave a permanent, unremovable entry in the explorer. What must hold is
    that the entry SITS in the workspace: its PARENT chain is therefore always
    resolved, and so is the entry itself unless it is the link being deleted.
    """
    clean = _normalized_relative(workspace_path, relative_path)
    if clean is None:
        return None

    full = os.path.join(workspace_path, clean)
    root = os.path.realpath(workspace_path)

    # The parent chain is followed: "link/secret.sql" must not slip out through
    # a link that points at a sibling directory.
    if not _is_inside(root, os.path.realpath(os.path.dirname(full))):
        logger.warning("path_escape_blocked", path=relative_path)
        return None

    if not os.path.islink(full) and not _is_inside(root, os.path.realpath(full)):
        logger.warning("path_escape_blocked", path=relative_path)
        return None

    return full


def _normalized_relative(workspace_path: str, relative_path: str) -> t.Optional[str]:
    """Reject the two shapes that are never a path inside the workspace."""
    clean = os.path.normpath(relative_path)
    if clean.startswith("..") or os.path.isabs(clean):
        logger.warning("path_traversal_blocked", path=relative_path)
        return None
    return clean


def _is_inside(root: str, candidate: str) -> bool:
    """Whether *candidate* is *root* itself or lives beneath it.

    Separator-aware on purpose: a bare ``startswith`` also accepts a SIBLING
    whose name merely extends the workspace's ("ws" vs "ws-backup"), and one
    symlink inside the copy is enough to reach it — the same prefix bug class
    as ws-x / ws-x-archive.
    """
    return candidate == root or candidate.startswith(root + os.sep)


def _matches_pattern(filename: str, pattern: str) -> bool:
    """Simple glob pattern match (supports *.ext)."""
    import fnmatch
    return fnmatch.fnmatch(filename, pattern)
