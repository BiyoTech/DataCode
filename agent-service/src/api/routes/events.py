"""SSE event streaming route."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["events"])


@router.get("/{session_id}/events")
async def sse_events(session_id: str, request: Request):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sq, token = event_bus.attach_subscriber(session_id)
    logger.info("sse_connected", session_id=session_id)

    async def _event_generator():
        try:
            async for event in event_bus.subscribe(sq):
                if event is None:
                    if await request.is_disconnected():
                        break
                    yield ": heartbeat\n\n"
                else:
                    yield f"event: {event.type.value}\ndata: {event.to_json()}\n\n"
        finally:
            event_bus.detach(session_id, token)
            logger.info("sse_disconnected", session_id=session_id)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
