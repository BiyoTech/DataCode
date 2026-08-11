"""File operation endpoints.

Handles file CRUD within workspaces:
- File writes mark the context as dirty for lazy refresh
- SQL files are optionally formatted on save (format_on_save)
- File change events are published via SSE for real-time UI updates
"""

from fastapi import APIRouter, HTTPException

from src.api.deps import WorkspacePath

from src.api.models.requests import DeleteFileRequest, MkdirRequest, RenameRequest, WriteFileRequest
from src.core.context_pool import get_context_pool
from src.core.event_bus import EventTopic, SSEEvent, get_event_bus
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _try_format_sql(content: str, workspace_path: str) -> str:
    """Attempt to format SQL content using the workspace's SQLMesh dialect.

    Returns formatted SQL on success, original content on failure.
    Matches SQLMesh web server's format_on_save behavior.
    """
    try:
        from sqlglot import parse_one, errors as sqlglot_errors

        pool = get_context_pool()
        entry = pool.get_entry_if_cached(workspace_path)
        dialect = "duckdb"
        if entry and entry.context:
            dialect = getattr(entry.context.config, "dialect", "duckdb") or "duckdb"

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
    from src.workspace.file_ops import write_file as _write

    content = req.content

    # Format SQL files on save (graceful: non-parseable SQL is saved as-is)
    if req.file_path.endswith(".sql"):
        content = _try_format_sql(content, req.workspace_path)

    full_path = _write(req.workspace_path, req.file_path, content)

    # Mark context dirty so next SQLMesh operation will refresh
    get_context_pool().mark_dirty(req.workspace_path)

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
    from src.workspace.file_ops import read_file as _read

    content = _read(workspace_path, file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content, "path": file_path}


@router.get("/list")
async def list_files(workspace_path: WorkspacePath, directory: str = "", recursive: bool = False, pattern: str = ""):
    """List files in a workspace directory."""
    from src.workspace.file_ops import list_files as _list

    files = _list(workspace_path, directory, recursive, pattern)
    return {
        "files": [
            {"path": f.path, "is_directory": f.is_directory, "size": f.size_bytes}
            for f in files
        ]
    }


@router.post("/delete")
async def delete_file(req: DeleteFileRequest):
    """Delete a file from a workspace. Marks context dirty and publishes event."""
    from src.workspace.file_ops import delete_file as _delete

    success = _delete(req.workspace_path, req.file_path)
    if not success:
        raise HTTPException(status_code=404, detail="File not found")

    # Mark context dirty
    get_context_pool().mark_dirty(req.workspace_path)

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
    from src.workspace.file_ops import create_directory as _mkdir

    path = _mkdir(req.workspace_path, req.directory)
    return {"success": True, "path": path}


@router.post("/rename")
async def rename_file(req: RenameRequest):
    """Rename/move a file or directory. Marks context dirty and publishes event."""
    from src.workspace.file_ops import rename_file as _rename

    new_path = _rename(req.workspace_path, req.old_path, req.new_path)

    # Mark context dirty
    get_context_pool().mark_dirty(req.workspace_path)

    # Publish file change event
    bus = get_event_bus()
    await bus.publish(SSEEvent(
        topic=EventTopic.FILE,
        data={"change": "renamed", "old_path": req.old_path, "new_path": req.new_path},
        workspace_path=req.workspace_path,
    ))

    return {"success": True, "path": new_path}
