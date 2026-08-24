"""Main agent chat execution pipeline."""

import asyncio

from datacode_common.logging.context import bind_workspace_context

from src.agents.registry import AgentRegistry
from src.api.schemas import ChatMessageRequest
from src.context.session import SessionContext, session_manager
from src.protocol.messages import (
    EventType,
    agent_start,
    create_event,
    system_error,
)
from src.runtime.goal_run import (
    build_agent_factory_kwargs,
    build_tool_context,
    finalize_goal_outcome,
    initial_registry_key_for_session,
    run_agent_goal_loop,
)
from src.runtime.memory_post import memory_post_process
from src.runtime.model_resolver import resolve_model
from src.runtime.resume import is_resume_intent, pop_pending_resume
from src.runtime.streaming import resume_after_max_turns
from src.runtime.timeline import publish_and_persist
from src.utils.logging import get_logger
from src.utils.tasks import spawn_background_task

logger = get_logger(__name__)


def _spawn_memory_post_process(session: SessionContext) -> None:
    """Kick off the post-turn memory summarization as a tracked background task."""
    spawn_background_task(
        memory_post_process(session),
        name=f"memory_post_process:{session.session_id}",
    )


def build_mention_context(mentions: list[dict]) -> str:
    if not mentions:
        return ""

    lines = ["[Referenced context]"]
    for m in mentions:
        mtype = m.get("type", "")
        mid = m.get("id", "")
        label = m.get("label", "")
        if mtype == "table":
            lines.append(f"- Table: {label} (FQN: {mid}) — available for schema discovery and query")
        elif mtype == "knowledge":
            lines.append(f"- Knowledge: {label} (id: {mid}) — business knowledge document")
        elif mtype == "adhoc":
            lines.append(f"- Adhoc Query: {label} (path: {mid}) — saved SQL query file")
        else:
            lines.append(f"- {label} ({mid})")
    return "\n".join(lines)


async def execute_chat(session: SessionContext, req: ChatMessageRequest) -> None:
    """Execute Agent pipeline and push events via EventBus."""
    session_id = session.session_id
    user_content = req.content
    mode = req.mode or session.mode.value
    model_id = req.model_id

    # Reserve a slot in active_chat_runs immediately so the LRU eviction logic
    # (which skips active sessions) considers this session "active" from the
    # very start. Without this, the window between get_session_async() and
    # run_agent_goal_loop()'s own registration could let the session be evicted
    # from the in-memory cache, causing get_messages() to return [] and the
    # agent to start with empty history. The real task reference is set below;
    # the done_callback in chat.py will clean this up on completion.
    from src.runtime.active_runs import active_chat_runs
    active_chat_runs[session_id] = {
        "task": asyncio.current_task(),
        "result": None,
        "agent_name": session.mode.value,
    }

    mention_ctx = build_mention_context(req.mentions)
    if mention_ctx:
        user_content = f"{mention_ctx}\n\n{user_content}"

    if mode and mode != session.mode.value:
        session_manager.set_mode(session_id, mode)

    # Resolve execution style (autonomy dimension). A per-message value overrides
    # the session default; otherwise the session's stored style is used.
    if req.execution_style:
        session_manager.set_execution_style(session_id, req.execution_style)
    execution_style = session.execution_style.value

    if req.workspace_path:
        session.workspace_path = req.workspace_path
        session.workspace_context = session_manager._init_workspace_context(req.workspace_path)
    if req.environment:
        session.environment = req.environment
    if req.workspace_path or req.environment:
        await session_manager.persist_runtime_context_async(session)

    # A chat run is its own asyncio task, so the context bound here covers the
    # whole agent loop — including tool calls — and lands its log lines in the
    # workspace the user is actually working in.
    if session.workspace_path:
        bind_workspace_context(
            session.workspace_path,
            user_id=session.user_id,
            environment=session.environment,
        )

    await session_manager.add_message_async(session_id, "user", user_content)
    await session_manager.add_timeline_event_async(
        session_id=session_id,
        event_type=EventType.MESSAGE_COMPLETE.value,
        event_id=create_event(EventType.MESSAGE_COMPLETE, session_id).event_id,
        parent_event_id="",
        payload={"role": "user", "content": user_content, "agent_name": ""},
    )

    if is_resume_intent(user_content):
        pending_resume = pop_pending_resume(session_id)
        if pending_resume is not None:
            # resume_after_max_turns now handles its own exceptions internally
            # (emits system_warning + agent_finish(error)) so it never re-raises.
            # The try/except here is kept only as a last-resort safety net.
            try:
                await resume_after_max_turns(session, pending_resume)
            except Exception as e:
                logger.error("resume_after_max_turns_unhandled", session_id=session_id, error=str(e), exc_info=True)
                await publish_and_persist(
                    session_id,
                    system_error(session_id, code="AGENT_ERROR", message=str(e)),
                )
            _spawn_memory_post_process(session)
            return
    else:
        pop_pending_resume(session_id)

    current_agent_name = "triage"

    try:
        # resolve_model may raise ValueError on misconfiguration (e.g. a custom
        # base_url without an api_key); keep it inside the try so it surfaces as
        # a system_error instead of crashing the handler.
        llm_model = resolve_model(model_id)

        workspace_ctx = session.workspace_context
        composed_memory = workspace_ctx.memory_index if workspace_ctx else ""
        if session.workspace_path:
            dynamic_l1 = await session_manager.load_dynamic_l1_memory(session.workspace_path)
            if dynamic_l1:
                composed_memory = dynamic_l1 + "\n" + composed_memory

        agent_mode = "craft" if session.scenario == "chatbi" else session.mode.value
        registry_key = initial_registry_key_for_session(session)
        agent_factory_kwargs = build_agent_factory_kwargs(
            session,
            agent_mode=agent_mode,
            composed_memory=composed_memory,
            llm_model=llm_model,
            execution_style=execution_style,
        )

        if session.scenario == "chatbi":
            current_agent_name = "data_analysis_agent"
        else:
            current_agent_name = "triage"

        entry_agent = AgentRegistry.get(registry_key, **agent_factory_kwargs)

        await publish_and_persist(
            session_id,
            agent_start(session_id, agent_name=current_agent_name, mode=agent_mode),
        )

        messages = session_manager.get_messages(session_id, workspace_path=session.workspace_path)
        tool_context = build_tool_context(session)

        run_outcome = await run_agent_goal_loop(
            session,
            entry_agent=entry_agent,
            initial_registry_key=registry_key,
            agent_factory_kwargs=agent_factory_kwargs,
            initial_input=messages,
            tool_context=tool_context,
            llm_model=llm_model,
            current_agent_name=current_agent_name,
        )

        if run_outcome.cancelled or run_outcome.paused:
            _spawn_memory_post_process(session)
            return

        await finalize_goal_outcome(session, run_outcome)

    except Exception as e:
        logger.error("agent_execution_error", session_id=session_id, error=str(e), exc_info=True)
        await publish_and_persist(
            session_id,
            system_error(session_id, code="AGENT_ERROR", message=str(e)),
        )
        _spawn_memory_post_process(session)
        return

    _spawn_memory_post_process(session)
