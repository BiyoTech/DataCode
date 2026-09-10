"""The ONLY way agent-service reaches a working copy's files.

Why this exists at all
----------------------
agent-service owns no working copy. The copy belongs to the workspace-runtime
replica holding its lease, and in every container deployment this service is
given no volume for it — so the file tools that read and wrote it directly
worked in host development and nowhere else (a chat could die with
``[Errno 13] Permission denied: '/data'`` before it started). Even where the
path happened to be reachable, touching it made this service a SECOND writer
inside a leased Git worktree, which is the one thing the lease exists to
prevent.

So every file operation crosses ``RuntimeClient`` to the replica that owns the
copy. This module is the typed façade over that: it turns HTTP payloads into
values the tools can reason about (missing / binary / truncated are ANSWERS,
not failures), and it validates the path the model supplied before spending a
round trip on it.

What stays here and what moves
------------------------------
Presentation stays: line windows, markdown rendering, diff computation and
office-document parsing are LLM-facing concerns and belong with the tools. The
walking, grepping and byte-level decisions moved to the service that has the
bytes.

Path validation is done on BOTH sides on purpose. The runtime's check is the
authoritative one (it is the trust boundary); the check here exists to give the
model an immediate, specific message instead of a generic upstream error, and
it is pure string work — nothing in this process may resolve a workspace path
against a real filesystem, because this process has none.
"""

from __future__ import annotations

import posixpath
import typing as t
from dataclasses import dataclass, field

from src.tools.runtime_client import RuntimeUnavailable, get_runtime_client
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Office documents are fetched whole (they are archives; a partial one cannot be
# parsed). The cap is what the runtime accepts and keeps one document from
# becoming a multi-hundred-megabyte base64 payload.
MAX_BINARY_BYTES = 20_000_000


class WorkspaceFileError(RuntimeError):
    """A file operation could not be performed against the working copy.

    Distinct from a NEGATIVE RESULT (missing file, no matches): those are
    returned as values. This means the operation did not happen — an unreachable
    runtime, a rejected path, a lease that could not be resolved — and the tool
    must tell the model the action did not take effect.
    """


@dataclass(frozen=True)
class TextFile:
    """A text read. ``missing`` and ``binary`` are results, not errors."""

    content: str
    missing: bool = False
    binary: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class FileEntry:
    """One listed entry, workspace-relative."""

    path: str
    is_directory: bool
    size_bytes: int = 0


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    text: str
    context: t.List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchResult:
    hits: t.List[SearchHit]
    files: t.Dict[str, int]
    truncated: bool


def normalize_relative_path(file_path: str) -> str:
    """Return *file_path* as a clean workspace-relative POSIX path.

    Raises:
        ValueError: the path is absolute or climbs out of the copy. Rejected
            here rather than sent on, so the model gets the actual reason.
    """
    raw = (file_path or "").strip()
    if not raw:
        raise ValueError("path is required")
    candidate = raw.replace("\\", "/")
    if candidate.startswith("/"):
        raise ValueError(f"path must be relative to the workspace root: {file_path}")
    clean = posixpath.normpath(candidate)
    if clean == ".." or clean.startswith("../"):
        raise ValueError(f"path is outside the workspace: {file_path}")
    return "" if clean == "." else clean


async def read_text(workspace_path: str, file_path: str) -> TextFile:
    """Read one file as text."""
    relative = normalize_relative_path(file_path)
    payload = await _call(
        "read", lambda client: client.read(workspace_path, relative)
    )
    return TextFile(
        content=str(payload.get("content", "")),
        missing=bool(payload.get("missing", False)),
        binary=bool(payload.get("binary", False)),
        truncated=bool(payload.get("truncated", False)),
    )


async def read_binary(
    workspace_path: str, file_path: str, *, max_bytes: int = MAX_BINARY_BYTES
) -> t.Optional[bytes]:
    """Read one file as raw bytes; ``None`` when it does not exist."""
    import base64
    import binascii

    relative = normalize_relative_path(file_path)
    payload = await _call(
        "read_binary",
        lambda client: client.read_binary(
            workspace_path, relative, max_bytes=max_bytes
        ),
    )
    if payload.get("missing"):
        return None
    try:
        return base64.b64decode(str(payload.get("content_base64", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkspaceFileError(
            f"workspace-runtime returned undecodable bytes for {file_path}: {exc}"
        ) from exc


async def write_text(workspace_path: str, file_path: str, content: str) -> str:
    """Create or overwrite one file. Returns the path the runtime wrote."""
    relative = normalize_relative_path(file_path)
    payload = await _call(
        "write", lambda client: client.write(workspace_path, relative, content)
    )
    return str(payload.get("path", relative))


async def delete_path(workspace_path: str, file_path: str) -> bool:
    """Delete one path. ``False`` means it was not there."""
    relative = normalize_relative_path(file_path)
    payload = await _call(
        "delete", lambda client: client.delete(workspace_path, relative)
    )
    return bool(payload.get("deleted", False))


async def list_entries(
    workspace_path: str,
    path: str = "",
    *,
    pattern: str = "*",
    recursive: bool = False,
    include_dirs: bool = True,
) -> t.List[FileEntry]:
    """List a directory of the working copy."""
    relative = normalize_relative_path(path) if path and path != "." else ""
    payload = await _call(
        "list",
        lambda client: client.list_dir(
            workspace_path,
            path=relative,
            pattern=pattern,
            recursive=recursive,
            include_dirs=include_dirs,
        ),
    )
    entries = payload.get("entries") or []
    return [
        FileEntry(
            path=str(entry.get("path", "")),
            is_directory=bool(entry.get("is_directory", False)),
            size_bytes=int(entry.get("size_bytes", 0) or 0),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("path")
    ]


async def search(
    workspace_path: str,
    query: str,
    *,
    file_pattern: str = "*",
    regex: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    max_results: int = 50,
    include_matches: bool = True,
) -> SearchResult:
    """Search file contents in the working copy."""
    payload = await _call(
        "search",
        lambda client: client.search(
            workspace_path,
            query,
            pattern=file_pattern,
            regex=regex,
            context_before=context_before,
            context_after=context_after,
            max_results=max_results,
            include_matches=include_matches,
        ),
    )
    hits = [
        SearchHit(
            path=str(item.get("path", "")),
            line=int(item.get("line", 0) or 0),
            text=str(item.get("text", "")),
            context=[str(line) for line in (item.get("context") or [])],
        )
        for item in (payload.get("matches") or [])
        if isinstance(item, dict)
    ]
    files = {
        str(path): int(count)
        for path, count in (payload.get("files") or {}).items()
    }
    return SearchResult(
        hits=hits, files=files, truncated=bool(payload.get("truncated", False))
    )


async def _call(
    operation: str, send: t.Callable[[t.Any], t.Awaitable[dict]]
) -> dict:
    """Run one runtime call, translating transport failure into our error.

    One funnel so every file operation reports unavailability the same way:
    the tools all have to say "this did not happen" in the same words, and the
    difference between a dead pod and a stale route is not something the model
    can act on.
    """
    client = get_runtime_client()
    if not client.configured:
        raise WorkspaceFileError(
            "workspace-runtime is not configured, so the working copy cannot be "
            "reached (conf/platform.yaml services.runtime.rest_url)"
        )
    try:
        return await send(client)
    except RuntimeUnavailable as exc:
        logger.warning("workspace_file_op_failed", operation=operation, error=str(exc))
        raise WorkspaceFileError(str(exc)) from exc
