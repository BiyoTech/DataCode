"""Chat, approval, and cancel routes."""

import asyncio

from fastapi import APIRouter, HTTPException

from src.api.schemas import ApprovalRequest, ChatMessageRequest
from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.protocol.messages import agent_finish, system_warning
from src.runtime.active_runs import (
    active_chat_runs,
    cancel_chat_run,
    register_chat_run,
    track_chat_run,
)
from src.runtime.chat_executor import execute_chat
from src.runtime.resume import is_resume_intent, peek_pending_resume
from src.runtime.session_coordination import get_session_coordination
from src.runtime.timeline import publish_and_persist
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def post_chat_message(session_id: str, req: ChatMessageRequest):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Resume forwarding: a "continue" message for a run paused on ANOTHER
    # replica must reach the replica holding the SDK run state — executing it
    # here would silently downgrade to a fresh (stateless) run. Checked before
    # the run registration below: the forwarded path registers on the owner.
    if (
        is_resume_intent(req.content)
        and peek_pending_resume(session_id) is None
        and await get_session_coordination().resume_pending_remotely(session_id)
    ):
        await get_session_coordination().broadcast_resume_chat(session_id, req.dict())
        logger.info("resume_chat_forwarded_to_owner", session_id=session_id)
        return {"status": "accepted", "session_id": session_id, "forwarded": True}

    # Serialize concurrent messages per session: if a run is already in flight,
    # reject the new request so two Runners never race on the same session's
    # messages / timeline / todo board. The frontend should wait for the current
    # run to finish (agent_finish / paused / error) before sending the next one.
    # The local dict answers the common case; the distributed registration
    # answers the case the dict cannot see — a run on ANOTHER replica.
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
    # SET NX in Redis: atomically claims the session across all replicas. A
    # lost race means another replica just started a run for this session.
    if not await register_chat_run(session_id):
        logger.info(
            "chat_run_already_in_progress_remote",
            session_id=session_id,
        )
        raise HTTPException(
            status_code=409,
            detail="A chat run is already in progress for this session",
        )

    task = asyncio.create_task(execute_chat(session, req))
    # track_chat_run owns everything that must follow the task: the local
    # handle, the registration heartbeat, and the release of the registration
    # when the task ends (whatever ends it).
    track_chat_run(session_id, task, agent_name=session.mode.value)
    return {"status": "accepted", "session_id": session_id}


@router.post("/{session_id}/approve")
async def post_approval(session_id: str, req: ApprovalRequest):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Resolution handles both approval kinds and, when the future lives on
    # another replica, routes the resolution there (broadcast + owner filter).
    outcome = await get_session_coordination().resolve_approval(
        session_id, req.request_id, req.approved, req.reason or ""
    )
    if outcome is None:
        await publish_and_persist(
            session_id,
            system_warning(
                session_id,
                code="APPROVAL_NOT_FOUND",
                message=f"No pending approval found for request_id={req.request_id}",
            ),
        )
        raise HTTPException(status_code=404, detail=f"Approval not found: {req.request_id}")

    if outcome.get("status") == "forwarded":
        # The owning replica resolves and publishes the human_response event
        # (SSE fan-out delivers it to any connected client).
        logger.info(
            "approval_resolution_forwarded",
            session_id=session_id,
            request_id=req.request_id,
        )
        return {
            "status": "forwarded",
            "request_id": req.request_id,
            "approved": req.approved,
        }

    if not req.approved:
        logger.info(
            "action_rejected",
            session_id=session_id,
            request_id=req.request_id,
            reason=req.reason,
        )

    return {
        "status": "resolved",
        "request_id": req.request_id,
        "approved": req.approved,
        **({"type": outcome["type"]} if "type" in outcome else {}),
    }


@router.post("/{session_id}/cancel")
async def post_cancel(session_id: str):
    session = await session_manager.get_session_async(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    logger.info("chat_cancel_received", session_id=session_id)
    # cancel_chat_run tries this replica first, then broadcasts to whichever
    # replica owns the run — a load-balanced cancel must still reach it.
    if await cancel_chat_run(session_id):
        return {"status": "cancelled", "session_id": session_id}

    await publish_and_persist(
        session_id,
        agent_finish(session_id, agent_name="triage", outcome="cancelled"),
    )
    return {"status": "cancelled", "session_id": session_id}
