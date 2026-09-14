"""Session management REST routes."""

import typing as t

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    CreateSessionRequest,
    SwitchExecutionStyleRequest,
    SwitchModeRequest,
    UpdateSessionRequest,
)
from src.config import settings
from src.context.session import session_manager
from src.runtime.active_runs import active_chat_runs, is_chat_running_remotely
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.post("/sessions")
async def create_session(req: CreateSessionRequest):
    session = await session_manager.create_session_async(
        user_id=req.user_id,
        title=req.title,
        mode=req.mode,
        scenario=req.scenario,
        execution_style=req.execution_style,
    )
    logger.info(
        "session_created_via_api",
        session_id=session.session_id,
        user_id=req.user_id,
        scenario=session.scenario,
        execution_style=session.execution_style.value,
    )
    return {
        "session_id": session.session_id,
        "title": session.title,
        "mode": session.mode.value,
        "execution_style": session.execution_style.value,
        "scenario": session.scenario,
        "messages": [],
        "timeline_events": [],
    }


@router.get("/sessions")
async def list_sessions(user_id: str, scenario: t.Optional[str] = None):
    sessions = await session_manager.list_sessions_async(user_id=user_id, scenario=scenario)
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.title,
                "mode": s.mode.value,
                "execution_style": s.execution_style.value,
                "scenario": s.scenario,
                "status": s.status,
                "message_count": s.message_count,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None,
            }
            for s in sessions
        ]
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = [
        {
            "id": f"msg-{session.session_id}-{i}",
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp", ""),
        }
        for i, msg in enumerate(session.messages)
    ]
    timeline_events = await session_manager.get_timeline_events_async(session.session_id)

    return {
        "session_id": session.session_id,
        "title": session.title,
        "mode": session.mode.value,
        "execution_style": session.execution_style.value,
        "scenario": session.scenario,
        "status": session.status,
        "message_count": session.message_count,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
        "messages": messages,
        "timeline_events": timeline_events,
        # Whether a chat run is in flight ANYWHERE (this replica's task dict ∪
        # the cross-replica registry). The frontend needs this to restore its
        # busy state on load/reconnect: the event stream alone cannot answer
        # "did I miss the agent.start?", because message.complete is emitted
        # mid-run (it seals one text segment, it does not end the run).
        "is_running": await _is_run_in_flight(session.session_id),
    }


async def _is_run_in_flight(session_id: str) -> bool:
    """Whether a chat run for this session exists on any replica.

    The local dict answers ownership (only a live task counts — a stale entry
    whose task finished is not a run); the registry answers existence across
    replicas, including this replica's own registration while the task lives.
    """
    run_info = active_chat_runs.get(session_id)
    task = run_info.get("task") if run_info else None
    if task is not None and not task.done():
        return True
    return await is_chat_running_remotely(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest):
    success = await session_manager.update_session_title_async(session_id, req.title)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "title": req.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    deleted = await session_manager.delete_session_async(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    full_messages = session.messages if session.messages else []
    result = [
        {
            "id": f"msg-{session_id}-{i}",
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp", ""),
        }
        for i, msg in enumerate(full_messages)
    ]
    timeline_events = await session_manager.get_timeline_events_async(session_id)
    return {"session_id": session_id, "messages": result, "timeline_events": timeline_events}


@router.post("/sessions/{session_id}/mode")
async def switch_mode(session_id: str, req: SwitchModeRequest):
    success = session_manager.set_mode(session_id, req.mode)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "mode": req.mode}


@router.post("/sessions/{session_id}/execution-style")
async def switch_execution_style(session_id: str, req: SwitchExecutionStyleRequest):
    success = await session_manager.set_execution_style_async(session_id, req.execution_style)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    session = await session_manager.get_session_async(session_id)
    return {
        "session_id": session_id,
        "execution_style": session.execution_style.value if session else req.execution_style,
    }


@router.get("/models")
async def list_models():
    return {
        "models": settings.llm.list_available_models(),
        "default_model": settings.llm.default_model,
    }


@router.get("/tools")
async def list_tools():
    from src.tools.registry import ToolRegistry

    return {"tools": ToolRegistry.list_tools()}
