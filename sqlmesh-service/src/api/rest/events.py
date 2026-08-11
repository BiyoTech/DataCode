"""SSE (Server-Sent Events) endpoint for real-time event streaming.

Matches SQLMesh web server's GET /api/events behavior:
- Clients connect via EventSource
- Receives plan progress, model changes, file changes, errors
- 15-second ping heartbeat keeps connection alive
- Per-workspace filtering via query parameter
"""

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from src.api.deps import validate_workspace_path
from src.core.event_bus import EventTopic, SSEEvent, get_event_bus
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["events"])

logger = get_logger(__name__)

HEARTBEAT_INTERVAL = 15  # seconds


async def _event_stream(
    request: Request,
    workspace_path: str,
) -> AsyncGenerator[str, None]:
    """Generate SSE event stream for a single client connection."""
    bus = get_event_bus()
    queue = bus.subscribe(workspace_path)

    try:
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                event: SSEEvent = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_INTERVAL
                )
                payload = json.dumps(event.data) if event.data is not None else "{}"
                yield f"event: {event.topic.value}\ndata: {payload}\n\n"
            except asyncio.TimeoutError:
                # Send ping heartbeat
                yield f"event: {EventTopic.PING.value}\ndata: {{}}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(queue, workspace_path)
        logger.debug("sse_client_disconnected", workspace_path=workspace_path or "(global)")


@router.get("/events")
async def sse_events(
    request: Request,
    workspace_path: str = "",
):
    """Server-Sent Events endpoint for real-time updates."""
    if workspace_path:
        workspace_path = validate_workspace_path(workspace_path)

    return StreamingResponse(
        _event_stream(request, workspace_path),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
