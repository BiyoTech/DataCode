"""Chat, approval, and cancel routes."""

import asyncio

from fastapi import APIRouter, HTTPException

from src.api.schemas import ApprovalRequest, ChatMessageRequest
from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.protocol.messages import agent_finish, human_response, system_warning
from src.runtime.approval import get_pending_approval, resolve_pending_approval
from src.runtime.active_runs import active_chat_runs
from src.runtime.chat_executor import execute_chat
from src.runtime.streaming import resume_after_approval
from src.runtime.timeline import publish_and_persist
from src.tools.command_approval import resolve_command_approval
from src.utils.logging import get_logger
from src.utils.tasks import spawn_background_task

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def post_chat_message(session_id: str, req: ChatMessageRequest):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Serialize concurrent messages per session: if a run is already in flight,
    # reject the new request so two Runners never race on the same session's
    # messages / timeline / todo board. The frontend should wait for the current
    # run to finish (agent_finish / paused / error) before sending the next one.
    run_info = active_chat_runs.get(session_id)
    if run_info is not None:
        task = run_info.get("task")
        if task is not None and not task.done():
            logger.info(
                "chat_run_already_in_progress",
                session_id=session_id,
            )
            raise HTTPException(
                status_code=409,
                detail="A chat run is already in progress for this session",
            )

    event_bus.ensure_queue(session_id)
    task = asyncio.create_task(execute_chat(session, req))
    active_chat_runs[session_id] = {
        "task": task,
        "result": None,
        "agent_name": session.mode.value,
    }

    # Clean up the run registry when the task finishes so active_chat_runs does
    # not grow unbounded, and surface any exception the task raised (a bare
    # create_task swallows exceptions until GC). Only remove the entry if it is
    # still the same task — a newer message may have replaced it.
    def _on_chat_done(t: asyncio.Task, sid: str = session_id) -> None:
        current = active_chat_runs.get(sid)
        if current is not None and current.get("task") is t:
            active_chat_runs.pop(sid, None)
        if t.cancelled():
            logger.info("chat_run_cancelled", session_id=sid)
            return
        exc = t.exception()
        if exc is not None:
            logger.error("chat_run_failed", session_id=sid, error=str(exc), exc_info=exc)

    task.add_done_callback(_on_chat_done)
    return {"status": "accepted", "session_id": session_id}


@router.post("/{session_id}/approve")
async def post_approval(session_id: str, req: ApprovalRequest):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    cmd_resolved = resolve_command_approval(session_id, req.request_id, req.approved)
    if cmd_resolved:
        logger.info(
            "command_approval_post_endpoint",
            session_id=session_id,
            request_id=req.request_id,
            approved=req.approved,
        )
        # Persist the resolution so a reloaded session recovers the approval state.
        await publish_and_persist(
            session_id,
            human_response(
                session_id,
                request_id=req.request_id,
                approved=req.approved,
                reason=req.reason or "",
            ),
        )
        return {
            "status": "resolved",
            "request_id": req.request_id,
            "approved": req.approved,
            "type": "command_approval",
        }

    pending = get_pending_approval(session_id, req.request_id)
    resolved = resolve_pending_approval(session_id, req.request_id, approved=req.approved)
    if not resolved:
        await publish_and_persist(
            session_id,
            system_warning(
                session_id,
                code="APPROVAL_NOT_FOUND",
                message=f"No pending approval found for request_id={req.request_id}",
            ),
        )
        raise HTTPException(status_code=404, detail=f"Approval not found: {req.request_id}")

    # Persist the resolution so a reloaded session recovers the approval state.
    await publish_and_persist(
        session_id,
        human_response(
            session_id,
            request_id=req.request_id,
            approved=req.approved,
            reason=req.reason or "",
        ),
    )

    if pending is not None:
        # Tracked background task: prevents premature GC and logs any failure.
        spawn_background_task(
            resume_after_approval(session, pending, req.approved),
            name=f"resume_after_approval:{session_id}",
        )

    if not req.approved:
        logger.info(
            "action_rejected",
            session_id=session_id,
            request_id=req.request_id,
            reason=req.reason,
        )

    return {"status": "resolved", "request_id": req.request_id, "approved": req.approved}


@router.post("/{session_id}/cancel")
async def post_cancel(session_id: str):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    logger.info("chat_cancel_received", session_id=session_id)
    run_info = active_chat_runs.get(session_id)
    if run_info:
        streamed = run_info.get("result")
        if streamed is not None and hasattr(streamed, "cancel"):
            streamed.cancel()
        task = run_info.get("task")
        if task and not task.done():
            task.cancel()
        return {"status": "cancelled", "session_id": session_id}

    await publish_and_persist(
        session_id,
        agent_finish(session_id, agent_name="triage", outcome="cancelled"),
    )
    return {"status": "cancelled", "session_id": session_id}
