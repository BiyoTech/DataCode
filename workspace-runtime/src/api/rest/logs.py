"""Workspace log endpoints.

The agent reads ``{workspace}/logs/`` with plain file tools, but the UI needs
bounded, filtered, merged reads, so these endpoints sit in front of the same
files: newest-first pages, an SSE tail, and access to the error archive.

Authorization is enforced upstream by the gateway under
``workspace:logs:read`` — deliberately not ``workspace:code:read``, because a
log line may quote SQL, data values or tool output that reading a model file
would not expose.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from src.api.deps import WorkspacePath, validate_workspace_path
from src.logs import reader
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])

logger = get_logger(__name__)

# Cadence of the tail poll. Files are append-only, so comparing sizes is enough
# and avoids platform-specific filesystem watchers.
POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 15.0


def _split(value: str) -> tuple[str, ...]:
    """Parse a comma-separated filter parameter."""
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


@router.get("")
async def list_logs(
    workspace_path: WorkspacePath,
    sources: str = "",
    levels: str = "",
    q: str = "",
    since: str = "",
    limit: int = reader.DEFAULT_LIMIT,
    cursor: str = "",
):
    """Return a newest-first page of log entries for one workspace."""
    page = await asyncio.to_thread(
        reader.query_logs,
        reader.LogQuery(
            workspace_path=workspace_path,
            sources=_split(sources),
            levels=_split(levels),
            search=q,
            since=since,
            limit=limit,
            cursor=cursor,
        ),
    )
    return {
        "entries": page.entries,
        "next_cursor": page.next_cursor,
        "sources": page.sources,
    }


async def _tail_stream(
    request: Request,
    workspace_path: str,
    sources: tuple[str, ...],
    levels: tuple[str, ...],
    search: str,
) -> AsyncGenerator[str, None]:
    """Emit lines appended after the connection opened."""
    offsets = await asyncio.to_thread(reader.tail_offsets, workspace_path)
    wanted_sources = set(sources)
    wanted_levels = set(levels)
    lowered = search.lower()
    idle = 0.0

    try:
        while True:
            if await request.is_disconnected():
                break

            entries = await asyncio.to_thread(
                reader.read_appended, workspace_path, offsets
            )
            emitted = False
            for entry in entries:
                if wanted_sources and entry["source"] not in wanted_sources:
                    continue
                if wanted_levels and entry["level"] not in wanted_levels:
                    continue
                if lowered and lowered not in json.dumps(entry).lower():
                    continue
                yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
                emitted = True

            idle = 0.0 if emitted else idle + POLL_INTERVAL
            if idle >= HEARTBEAT_INTERVAL:
                idle = 0.0
                yield "event: ping\ndata: {}\n\n"

            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        pass
    finally:
        logger.debug("log_tail_disconnected", workspace_path=workspace_path)


@router.get("/stream")
async def stream_logs(
    request: Request,
    workspace_path: str = "",
    sources: str = "",
    levels: str = "",
    q: str = "",
):
    """Server-Sent Events tail of the workspace log directory."""
    resolved = validate_workspace_path(workspace_path)
    return StreamingResponse(
        _tail_stream(request, resolved, _split(sources), _split(levels), q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/errors")
async def list_archived_errors(workspace_path: WorkspacePath, limit: int = 50):
    """List archived errors newest-first, including the legacy location."""
    errors = await asyncio.to_thread(reader.list_errors, workspace_path, limit)
    return {"errors": errors}


@router.get("/errors/{error_id}")
async def read_archived_error(error_id: str, workspace_path: WorkspacePath):
    """Return one archived error with its full traceback and context."""
    payload = await asyncio.to_thread(reader.read_error, workspace_path, error_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Error archive entry not found")
    return payload
