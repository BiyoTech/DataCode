"""File operation endpoints.

Handles file CRUD within workspaces:
- File writes mark the context as dirty for lazy refresh
- SQL files are optionally formatted on save (format_on_save)
- File change events are published via SSE for real-time UI updates
"""

from fastapi import APIRouter, HTTPException

from src.api.deps import Caller, WorkspacePath
from src.api.models.requests import (
    DeleteDraftRequest,
    DeleteFileRequest,
    MkdirRequest,
    RenameRequest,
    SaveDraftRequest,
    WriteFileRequest,
)
from src.executor.process import get_executor_manager
from src.sqlmesh.event_bus import EventTopic, SSEEvent, get_event_bus
from src.sqlmesh.metadata_context import get_metadata_context
from src.utils.errors import PermissionDeniedError
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _assert_working_copy_owner(caller_user_id: str, workspace_path: str) -> None:
    """Drafts are one user's private typing state, scoped to their copy.

    The gateway authorizes workspace-scoped access; this is the finer rule —
    members can read each other's code, but nobody else reads or overwrites MY
    unsaved keystrokes.
    """
    from src.workspace.identity import resolve_workspace_identity

    owner = resolve_workspace_identity(workspace_path).user_id
    if owner != caller_user_id:
        raise PermissionDeniedError(
            "Only the working copy's owner may access its editor drafts"
        )


def _try_format_sql(content: str, workspace_path: str) -> str:
    """Attempt to format SQL content using the workspace's SQLMesh dialect.

    Returns formatted SQL on success, original content on failure.
    Matches SQLMesh web server's format_on_save behavior.

    The dialect comes from the parsed config — NOT from a Context, which would
    execute the user's models just to know how to pretty-print a file.
    """
    try:
        from sqlglot import parse_one

        from src.sqlmesh.config_access import gateway_dialect, load_workspace_config

        try:
            config = load_workspace_config(workspace_path)
            dialect = gateway_dialect(config) or "duckdb"
        except Exception:  # noqa: BLE001 - formatting must never block a save
            dialect = "duckdb"

        parsed = parse_one(content, dialect=dialect)
        formatted = parsed.sql(pretty=True, dialect=dialect)
        return formatted
    except Exception:
        # Return original content if formatting fails (syntax errors, etc.)
        return content


@router.post("/write")
async def write_file(req: WriteFileRequest):
    """Write a file to a workspace.

    For .sql files, applies format_on_save if content is parseable.
    Marks context dirty for lazy refresh and publishes file change event.
    """
    from src.files.ops import write_file as _write

    content = req.content

    # Format SQL files on save (graceful: non-parseable SQL is saved as-is)
    if req.file_path.endswith(".sql"):
        content = _try_format_sql(content, req.workspace_path)

    full_path = _write(req.workspace_path, req.file_path, content)

    # Mark both context holders dirty: the executor sandbox (its warm Context
    # cannot see the write) and the runtime's cached metadata (its payloads
    # describe the pre-write tree).
    get_executor_manager().mark_context_dirty(req.workspace_path)
    get_metadata_context().mark_dirty(req.workspace_path)

    # Publish file change event via SSE
    bus = get_event_bus()
    await bus.publish(SSEEvent(
        topic=EventTopic.FILE,
        data={"change": "modified", "path": req.file_path},
        workspace_path=req.workspace_path,
    ))

    return {"success": True, "path": full_path}


@router.get("/read")
async def read_file(workspace_path: WorkspacePath, file_path: str):
    """Read a file from a workspace."""
    from src.files.ops import read_file as _read

    content = _read(workspace_path, file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content, "path": file_path}


@router.get("/list")
async def list_files(
    workspace_path: WorkspacePath,
    directory: str = "",
    recursive: bool = False,
    pattern: str = "",
):
    """List files in a workspace directory."""
    from src.files.ops import list_files as _list

    files = _list(workspace_path, directory, recursive, pattern)
    return {
        "files": [
            {"path": f.path, "is_directory": f.is_directory, "size": f.size_bytes}
            for f in files
        ]
    }


@router.post("/delete")
async def delete_file(req: DeleteFileRequest):
    """Delete a file, symlink or directory. Marks context dirty, publishes event.

    A directory takes everything under it with it and is not recoverable; the
    explorer confirms with the user before sending the request.
    """
    from src.files.ops import delete_path

    success = delete_path(req.workspace_path, req.file_path)
    if not success:
        raise HTTPException(status_code=404, detail="Path not found")

    # Mark both context holders dirty (sandbox warm Context + metadata cache)
    get_executor_manager().mark_context_dirty(req.workspace_path)
    get_metadata_context().mark_dirty(req.workspace_path)

    # Publish file change event
    bus = get_event_bus()
    await bus.publish(SSEEvent(
        topic=EventTopic.FILE,
        data={"change": "deleted", "path": req.file_path},
        workspace_path=req.workspace_path,
    ))

    return {"success": True}


@router.post("/mkdir")
async def create_directory(req: MkdirRequest):
    """Create a directory in a workspace."""
    from src.files.ops import create_directory as _mkdir

    path = _mkdir(req.workspace_path, req.directory)
    return {"success": True, "path": path}


@router.post("/rename")
async def rename_file(req: RenameRequest):
    """Rename/move a file or directory. Marks context dirty and publishes event."""
    from src.files.ops import rename_file as _rename

    new_path = _rename(req.workspace_path, req.old_path, req.new_path)

    # Mark both context holders dirty (sandbox warm Context + metadata cache)
    get_executor_manager().mark_context_dirty(req.workspace_path)
    get_metadata_context().mark_dirty(req.workspace_path)

    # Publish file change event
    bus = get_event_bus()
    await bus.publish(SSEEvent(
        topic=EventTopic.FILE,
        data={"change": "renamed", "old_path": req.old_path, "new_path": req.new_path},
        workspace_path=req.workspace_path,
    ))

    return {"success": True, "path": new_path}


# ── Editor draft autosave (design 07 Q6: the <=5 s layer) ────────────────────
#
# The working-copy layer (WIP snapshots) bounds loss at ten minutes for written
# content; drafts bound what was typed but not yet saved, and live in the
# database so they survive the loss of a replica's disk entirely. They are
# deliberately NOT part of the working copy: writing them to disk would mark
# the copy dirty and pollute every snapshot with half-finished keystrokes.


@router.put("/draft")
async def save_draft(req: SaveDraftRequest, caller: Caller):
    """Upsert the draft for one file (the frontend debounces at 5 s)."""
    from src.workspace.repository import FileDraftRepository

    _assert_working_copy_owner(caller.user_id, req.workspace_path)
    stamp = await FileDraftRepository().upsert(
        req.workspace_path, req.file_path, req.content
    )
    return {"saved": True, "updated_at": stamp.isoformat()}


@router.get("/draft")
async def get_draft(workspace_path: WorkspacePath, file_path: str, caller: Caller):
    """Fetch the draft for one file; 404 when none exists (the normal case)."""
    from src.workspace.repository import FileDraftRepository

    _assert_working_copy_owner(caller.user_id, workspace_path)
    draft = await FileDraftRepository().get(workspace_path, file_path)
    if draft is None:
        raise HTTPException(status_code=404, detail="No draft for this file")
    return {"content": draft.content, "updated_at": draft.updated_at.isoformat()}


@router.post("/draft/delete")
async def delete_draft(req: DeleteDraftRequest, caller: Caller):
    """Drop the draft after its file was saved or its recovery discarded.

    POST rather than DELETE because the identifiers travel in the JSON body
    (the gateway's DELETE proxy does not forward one). Idempotent: deleting a
    draft that is already gone is what the caller wanted.
    """
    from src.workspace.repository import FileDraftRepository

    _assert_working_copy_owner(caller.user_id, req.workspace_path)
    deleted = await FileDraftRepository().delete(
        req.workspace_path, req.file_path
    )
    return {"deleted": deleted}
