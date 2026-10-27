"""Agent run streaming, persistence, and resume handlers."""

import asyncio
import json as _json_module
import time
import typing as t
from dataclasses import dataclass

from agents.items import ItemHelpers, ToolApprovalItem
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from src.agents.registry import AgentRegistry
from src.context.event_bus import event_bus
from src.context.execution_style import ExecutionPolicy, get_policy
from src.context.session import SessionContext, session_manager
from src.context.tool_context import ToolContext
from src.protocol.messages import (
    agent_delegate,
    agent_finish,
    agent_start,
    artifact_created,
    human_required,
    message_complete,
    message_delta,
    system_warning,
    thought_update,
    tool_end,
    tool_start,
)
from src.runtime.approval import PendingApproval, register_pending_approval
from src.runtime.artifacts import FILE_WRITING_TOOLS, guess_language
from src.runtime.policy_resolver import resolve_default_tool_level
from src.runtime.resume import PendingResume, register_pending_resume
from src.runtime.timeline import publish_and_persist
from src.tools.registry import ToolRegistry, risk_badge_for_level
from src.utils.logging import get_logger

logger = get_logger(__name__)


def resolve_risk_badge(tool_level: int) -> tuple[str, str]:
    return risk_badge_for_level(tool_level)


async def publish_tool_approval_request(
    session: SessionContext,
    current_agent_name: str,
    streamed_result: t.Any,
) -> bool:
    interruptions = getattr(streamed_result, "interruptions", []) or []
    if not interruptions:
        return False

    approval_item = next(
        (item for item in interruptions if isinstance(item, ToolApprovalItem)),
        None,
    )
    if approval_item is None:
        return False

    tool_name = approval_item.name or "unknown"
    tool_level = ToolRegistry.get_tool_level(
        tool_name,
        default=resolve_default_tool_level(session),
    )
    risk_badge_label, risk_badge_tone = resolve_risk_badge(tool_level)
    request_id = approval_item.call_id or f"approval-{session.session_id}-{tool_name}"
    description = f"Approve tool execution: {tool_name}"
    run_state = streamed_result.to_state()
    pending = PendingApproval(
        request_id=request_id,
        session_id=session.session_id,
        tool_name=tool_name,
        description=description,
        run_state=run_state,
        approval_item=approval_item,
    )
    register_pending_approval(pending)

    await publish_and_persist(
        session.session_id,
        human_required(
            session.session_id,
            request_id=request_id,
            request_type="tool_approval",
            title=f"Approve {tool_name}",
            description=description,
            tool_name=tool_name,
            tool_level=tool_level,
            risk_badge_label=risk_badge_label,
            risk_badge_tone=risk_badge_tone,
        ),
    )
    return True


@dataclass
class _TextStreamProfile:
    """Shape of the model's text stream, as observed by this service.

    Whether the UI types out an answer or drops it in one block is decided by
    the provider, not by this pipeline: every delta is published and flushed
    immediately. This records the evidence needed to tell the two apart from a
    log line — a provider that buffers its response shows up as a handful of
    deltas whose largest is the whole answer, spanning almost no time.
    """

    deltas: int = 0
    chars: int = 0
    largest_delta: int = 0
    first_delta_at: float = 0.0
    last_delta_at: float = 0.0

    def record(self, delta_text: str, now: float) -> None:
        if not self.deltas:
            self.first_delta_at = now
        self.deltas += 1
        self.chars += len(delta_text)
        self.largest_delta = max(self.largest_delta, len(delta_text))
        self.last_delta_at = now

    def log(self, session_id: str, agent_name: str, started_at: float) -> None:
        if not self.deltas:
            return
        logger.info(
            "model_text_stream_profile",
            session_id=session_id,
            agent_name=agent_name,
            deltas=self.deltas,
            chars=self.chars,
            largest_delta=self.largest_delta,
            # Time to the first token and the span the tokens covered. A
            # buffered provider yields span_ms ~ 0 with ttft_ms ~ the whole
            # model latency.
            ttft_ms=int((self.first_delta_at - started_at) * 1000),
            span_ms=int((self.last_delta_at - self.first_delta_at) * 1000),
        )


async def stream_agent_run(
    session: SessionContext,
    streamed_result: t.Any,
    current_agent_name: str,
    tool_context: ToolContext,
    *,
    seed_tool_calls: t.Optional[t.Dict[str, t.Dict[str, t.Any]]] = None,
) -> tuple[str, str, bool]:
    full_response = ""
    # Text accumulated for the CURRENT streaming segment (since the last flush).
    # A single turn can emit "text -> tool_call -> text -> ...". Each contiguous
    # run of text deltas must be sealed with its own ``message.complete`` before
    # the next structured item (tool call / handoff / reasoning) is published;
    # otherwise the frontend's streaming bubble is never finalized and gets stuck
    # showing partial/typewriter-truncated text (e.g. "已确认用方案 A。我").
    segment_text = ""

    async def _flush_streaming_text() -> None:
        """Seal the in-flight text segment as a ``message.complete`` event.

        Called before emitting any non-text structured item so the UI can
        finalize the streaming bubble (typewriter converges to the full text)
        instead of leaving it half-rendered.
        """
        nonlocal segment_text
        if not segment_text:
            return
        await publish_and_persist(
            session.session_id,
            message_complete(
                session.session_id,
                agent_name=current_agent_name,
                content=segment_text,
            ),
        )
        segment_text = ""

    # Seed the cache with in-flight tool calls carried over from a previous
    # segment (e.g. a tool paused for human approval). On resume, the SDK only
    # streams the ``tool_call_output_item`` — the matching ``tool_call_item``
    # lives in the prior segment and is NOT re-emitted. Without the seed the
    # output item cannot resolve its tool name/arguments, leaving the UI tool
    # card stuck "running" and skipping artifact emission.
    tool_call_cache: t.Dict[str, t.Dict[str, t.Any]] = dict(seed_tool_calls or {})
    approval_emitted = False
    profile = _TextStreamProfile()
    stream_started_at = time.monotonic()

    async for event in streamed_result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                delta_text = event.data.delta
                full_response += delta_text
                segment_text += delta_text
                profile.record(delta_text, time.monotonic())
                await event_bus.publish(
                    session.session_id,
                    message_delta(
                        session.session_id,
                        agent_name=current_agent_name,
                        delta=delta_text,
                    ),
                )
                # Yield control back to the event loop so the SSE consumer can flush this
                # delta right away. event_bus.publish only does a non-blocking put_nowait and
                # never suspends; without an explicit yield, a burst of buffered deltas would
                # be enqueued back-to-back and only reach the client all at once.
                await asyncio.sleep(0)

        elif isinstance(event, RunItemStreamEvent):
            item = event.item

            if item.type == "tool_call_item":
                # Seal any text streamed before this tool call so the bubble is
                # finalized rather than left mid-typewriter.
                await _flush_streaming_text()
                tool_name = ""
                arguments = {}
                call_id = ""
                if hasattr(item, "raw_item"):
                    tool_name = getattr(item.raw_item, "name", "") or ""
                    call_id = getattr(item.raw_item, "call_id", "") or ""
                    raw_args = getattr(item.raw_item, "arguments", "")
                    if raw_args:
                        try:
                            arguments = _json_module.loads(raw_args)
                        except (ValueError, TypeError):
                            arguments = {}
                if call_id:
                    tool_call_cache[call_id] = {"name": tool_name, "arguments": arguments}
                await publish_and_persist(
                    session.session_id,
                    tool_start(
                        session.session_id,
                        tool_name=tool_name,
                        agent_name=current_agent_name,
                        arguments=arguments,
                    ),
                )

            elif item.type == "tool_call_output_item":
                call_id = ""
                if hasattr(item, "raw_item"):
                    raw = item.raw_item
                    call_id = getattr(raw, "call_id", "") or ""
                    if not call_id and isinstance(raw, dict):
                        call_id = raw.get("call_id", "")
                cached = tool_call_cache.pop(call_id, {})
                tool_name = cached.get("name", "")
                arguments = cached.get("arguments", {})
                output = getattr(item, "output", "")
                output_str = str(output)[:2000] if output else ""

                await publish_and_persist(
                    session.session_id,
                    tool_end(session.session_id, tool_name=tool_name, result=output_str),
                )

                if tool_name in FILE_WRITING_TOOLS and arguments.get("file_path"):
                    file_path = arguments.get("file_path", "")
                    await publish_and_persist(
                        session.session_id,
                        artifact_created(
                            session.session_id,
                            file_path=file_path,
                            content=arguments.get("content", ""),
                            language=guess_language(file_path),
                        ),
                    )

            elif item.type == "reasoning_item":
                reasoning_text = ""
                if hasattr(item, "raw_item"):
                    raw = item.raw_item
                    content = getattr(raw, "content", None) or getattr(raw, "summary", None)
                    if isinstance(content, str):
                        reasoning_text = content
                    elif isinstance(content, list):
                        parts = [getattr(c, "text", "") or "" for c in content]
                        reasoning_text = " ".join(p for p in parts if p)
                if reasoning_text:
                    # Seal preceding text before showing a reasoning step.
                    await _flush_streaming_text()
                    await publish_and_persist(
                        session.session_id,
                        thought_update(
                            session.session_id,
                            agent_name=current_agent_name,
                            thought=reasoning_text,
                        ),
                    )

            elif item.type == "message_output_item":
                # The SDK emits the finalized text for a completed assistant
                # message here. It corresponds exactly to the text we streamed
                # via deltas since the last flush, so use it to seal the current
                # segment (authoritative over the delta-accumulated copy). Do NOT
                # overwrite ``full_response`` wholesale — that dropped earlier
                # text segments when a turn produced multiple messages.
                if hasattr(item, "raw_item") and hasattr(item.raw_item, "content"):
                    for content_block in item.raw_item.content:
                        if hasattr(content_block, "text"):
                            if content_block.text != segment_text:
                                # The two should be identical: the SDK builds
                                # this text by concatenating the same deltas we
                                # streamed. A mismatch means the provider's
                                # stream and its final message disagree, which
                                # the user sees as text that changes or repeats
                                # itself once the bubble is finalized.
                                logger.warning(
                                    "model_final_text_differs_from_stream",
                                    session_id=session.session_id,
                                    agent_name=current_agent_name,
                                    streamed_chars=len(segment_text),
                                    final_chars=len(content_block.text),
                                )
                            segment_text = content_block.text

        elif isinstance(event, AgentUpdatedStreamEvent):
            # Seal any text from the current agent before handing off, so its
            # bubble is finalized instead of stuck mid-stream.
            await _flush_streaming_text()
            new_agent = event.new_agent
            new_agent_name = getattr(new_agent, "name", "unknown")
            await publish_and_persist(
                session.session_id,
                agent_delegate(
                    session.session_id,
                    from_agent=current_agent_name,
                    to_agent=new_agent_name,
                    reason="Routing to specialist",
                ),
            )
            current_agent_name = new_agent_name
            await publish_and_persist(
                session.session_id,
                agent_start(
                    session.session_id,
                    agent_name=current_agent_name,
                    mode=session.mode.value,
                ),
            )

    profile.log(session.session_id, current_agent_name, stream_started_at)

    if getattr(streamed_result, "interruptions", []):
        approval_emitted = await publish_tool_approval_request(
            session, current_agent_name, streamed_result
        )

    # Return only the trailing (un-flushed) text segment. Text that was already
    # sealed mid-stream as its own ``message.complete`` must NOT be re-emitted by
    # the goal loop's finalizer — doing so would duplicate the message.
    return segment_text, current_agent_name, approval_emitted


def extract_new_run_items(result: t.Any) -> t.List[t.Any]:
    try:
        full = result.to_input_list()
        base = ItemHelpers.input_to_new_input_list(result.input)
        return list(full[len(base) :])
    except Exception as e:
        logger.warning("extract_new_run_items_failed", error=str(e))
        return []


def resolve_segment_turns_used(result: t.Any, allocated_max_turns: int) -> int:
    """Return turns actually consumed in a Runner segment.

    The SDK exposes ``current_turn`` on streaming results. We must not charge the
    full *allocated* budget when the model stopped early — that was starving
    auto-resume and todo-continuation budgets.

    Fallback chain (each step is tried only if the previous yields nothing):
      1. ``current_turn``  — public field on ``RunResultStreaming`` (SDK ≥ 0.8).
      2. ``new_items``     — inherited from ``RunResultBase``; count items as a
                             turn proxy (each turn produces ≥ 1 item).
      3. ``is_complete``   — streaming finished with no items → 1 turn.
      4. ``1``             — conservative default; never charge the full
                             allocated budget for an unknown result shape.
    """
    current_turn = getattr(result, "current_turn", None)
    if isinstance(current_turn, int) and current_turn > 0:
        if allocated_max_turns > 0:
            return min(current_turn, allocated_max_turns)
        return current_turn

    new_items = getattr(result, "new_items", None)
    if new_items:
        return max(1, len(new_items))

    if getattr(result, "is_complete", False):
        return 1

    # Conservative: charge 1 turn rather than the full allocated budget.
    # The previous pessimistic fallback (allocated_max_turns) re-introduced
    # the budget-starvation bug when the SDK result shape didn't match.
    return 1


async def persist_new_run_items(session: SessionContext, result: t.Any) -> None:
    new_items = extract_new_run_items(result)
    if new_items:
        await session_manager.persist_run_items_async(session.session_id, new_items)


async def handle_max_turns_exceeded(
    session: SessionContext,
    result: t.Any,
    current_agent_name: str,
    *,
    policy: t.Optional[ExecutionPolicy] = None,
    auto_resume_attempted: bool = False,
) -> None:
    policy = policy or get_policy(session.execution_style)

    run_state = None
    try:
        run_state = result.to_state()
    except Exception as e:
        logger.warning("max_turns_to_state_failed", session_id=session.session_id, error=str(e))
    if run_state is not None:
        register_pending_resume(
            PendingResume(
                session_id=session.session_id,
                run_state=run_state,
                current_agent_name=current_agent_name,
            )
        )

    segment_limit = policy.max_turns_per_run
    logger.warning(
        "max_turns_exceeded",
        session_id=session.session_id,
        current_agent=current_agent_name,
        resumable=run_state is not None,
        max_turns=segment_limit,
        execution_style=policy.style.value,
        auto_resume_attempted=auto_resume_attempted,
    )

    if policy.auto_resume_on_max_turns and auto_resume_attempted:
        message = (
            f"Paused after {policy.max_auto_resumes} automatic resume segment(s) "
            f"(limit {policy.max_total_turns} total turns). "
            "The task may still be incomplete."
        )
        suggestion = "Reply '继续' (continue) to resume, or send a new instruction."
    elif policy.auto_resume_on_max_turns:
        message = (
            f"Reached the per-segment turn limit ({segment_limit}). "
            "Automatic resume is disabled or the resume budget is exhausted."
        )
        suggestion = "Reply '继续' (continue) to resume from where the agent stopped."
    else:
        message = (
            f"Reached the per-segment turn limit ({segment_limit}). "
            "The task was paused before completion."
        )
        suggestion = "Reply '继续' (continue) to resume from where the agent stopped."

    await publish_and_persist(
        session.session_id,
        system_warning(
            session.session_id,
            code="MAX_TURNS_EXCEEDED",
            message=message,
            suggestion=suggestion,
        ),
    )
    await publish_and_persist(
        session.session_id,
        agent_finish(
            session.session_id,
            agent_name=current_agent_name,
            outcome="paused",
            summary="Paused: max turns reached. Reply to resume.",
        ),
    )


async def handle_turn_budget_exhausted(
    session: SessionContext,
    current_agent_name: str,
    policy: ExecutionPolicy,
) -> None:
    """Total turn budget for this goal is exhausted before the next segment."""
    logger.warning(
        "turn_budget_exhausted",
        session_id=session.session_id,
        current_agent=current_agent_name,
        max_total_turns=policy.max_total_turns,
        execution_style=policy.style.value,
    )
    await publish_and_persist(
        session.session_id,
        system_warning(
            session.session_id,
            code="TURN_BUDGET_EXHAUSTED",
            message=(
                f"Total turn budget exhausted ({policy.max_total_turns} turns). "
                "The task was paused before completion."
            ),
            suggestion="Reply '继续' (continue) or send a follow-up to keep going.",
        ),
    )
    await publish_and_persist(
        session.session_id,
        agent_finish(
            session.session_id,
            agent_name=current_agent_name,
            outcome="paused",
            summary="Paused: total turn budget exhausted.",
        ),
    )


def _seed_tool_calls_from_approval(
    pending: PendingApproval,
) -> t.Dict[str, t.Dict[str, t.Any]]:
    """Rebuild tool-call metadata for the approved call.

    The resumed stream emits the tool's output item without a preceding
    ``tool_call_item`` (that item lives in the paused segment). Seeding the
    stream cache with ``{call_id: {name, arguments}}`` lets the output item
    correlate back to the original tool, so the UI card resolves and artifacts
    are emitted correctly.
    """
    item = pending.approval_item
    call_id = getattr(item, "call_id", None)
    if not call_id:
        return {}

    arguments: t.Dict[str, t.Any] = {}
    raw_args = getattr(item, "arguments", None)
    if raw_args:
        try:
            arguments = _json_module.loads(raw_args)
        except (ValueError, TypeError):
            arguments = {}

    tool_name = getattr(item, "name", None) or pending.tool_name
    return {call_id: {"name": tool_name, "arguments": arguments}}


async def resume_after_approval(
    session: SessionContext,
    pending: PendingApproval,
    approved: bool,
) -> None:
    from src.runtime.goal_run import (
        build_agent_factory_kwargs,
        build_tool_context,
        finalize_goal_outcome,
        initial_registry_key_for_session,
        run_agent_goal_loop,
    )
    from src.runtime.model_resolver import resolve_model

    # Record the approval decision on the run_state's OWN context. This context
    # is preserved on resume (goal loop passes context=None for RunState input),
    # so the SDK sees the approved call and actually executes the tool.
    if approved:
        pending.run_state.approve(pending.approval_item)
    else:
        pending.run_state.reject(pending.approval_item)

    workspace_ctx = session.workspace_context
    tool_context = build_tool_context(session)
    current_agent_name = getattr(pending.run_state._current_agent, "name", "triage")

    # Re-announce the agent so the UI leaves the "awaiting approval" paused
    # state and shows the run as active again while the approved tool executes.
    # Without this, the frontend keeps its idle/"completed" appearance because
    # the pause emitted a human.required event that cleared the running state.
    await publish_and_persist(
        session.session_id,
        agent_start(
            session.session_id,
            agent_name=current_agent_name,
            mode=session.mode.value,
        ),
    )

    agent_mode = "craft" if session.scenario == "chatbi" else session.mode.value
    registry_key = initial_registry_key_for_session(session)
    agent_factory_kwargs = build_agent_factory_kwargs(
        session,
        agent_mode=agent_mode,
        composed_memory=workspace_ctx.memory_index if workspace_ctx else "",
        llm_model=resolve_model(""),
        execution_style=session.execution_style.value,
    )
    entry_agent = pending.run_state._current_agent or AgentRegistry.get(
        registry_key, **agent_factory_kwargs
    )

    # This runs as a detached background task, so an unhandled exception would be
    # swallowed and leave the UI spinning forever. Guard it: log the failure and
    # emit a terminal finish event so the frontend leaves its running state.
    try:
        outcome = await run_agent_goal_loop(
            session,
            entry_agent=entry_agent,
            initial_registry_key=registry_key,
            agent_factory_kwargs=agent_factory_kwargs,
            initial_input=pending.run_state,
            tool_context=tool_context,
            llm_model=resolve_model(""),
            current_agent_name=current_agent_name,
            seed_tool_calls=_seed_tool_calls_from_approval(pending),
        )
        await finalize_goal_outcome(session, outcome)
    except Exception as e:  # noqa: BLE001 - background task must never die silently
        logger.exception(
            "resume_after_approval_failed",
            session_id=session.session_id,
            request_id=pending.request_id,
            tool_name=pending.tool_name,
            error=str(e),
        )
        await publish_and_persist(
            session.session_id,
            system_warning(
                session.session_id,
                code="RESUME_FAILED",
                message=f"Failed to resume after approval: {e}",
                suggestion="Please retry the request.",
            ),
        )
        await publish_and_persist(
            session.session_id,
            agent_finish(
                session.session_id,
                agent_name=current_agent_name,
                outcome="error",
                summary="Resume after approval failed.",
            ),
        )


async def resume_after_max_turns(session: SessionContext, pending: PendingResume) -> None:
    from src.runtime.goal_run import (
        build_agent_factory_kwargs,
        build_tool_context,
        finalize_goal_outcome,
        initial_registry_key_for_session,
        run_agent_goal_loop,
    )
    from src.runtime.model_resolver import resolve_model

    workspace_ctx = session.workspace_context
    tool_context = build_tool_context(session)
    current_agent_name = pending.current_agent_name or "triage"
    logger.info(
        "resume_from_run_state",
        session_id=session.session_id, current_agent=current_agent_name,
    )

    await publish_and_persist(
        session.session_id,
        agent_start(
            session.session_id,
            agent_name=current_agent_name,
            mode=session.mode.value,
        ),
    )

    agent_mode = "craft" if session.scenario == "chatbi" else session.mode.value
    registry_key = initial_registry_key_for_session(session)
    llm_model = resolve_model("")
    agent_factory_kwargs = build_agent_factory_kwargs(
        session,
        agent_mode=agent_mode,
        composed_memory=workspace_ctx.memory_index if workspace_ctx else "",
        llm_model=llm_model,
        execution_style=session.execution_style.value,
    )
    entry_agent = pending.run_state._current_agent or AgentRegistry.get(
        registry_key, **agent_factory_kwargs
    )

    # Mirrors resume_after_approval: this runs as a detached background task,
    # so an unhandled exception would be swallowed and leave the UI spinning
    # forever. Guard it: log the failure and emit a terminal finish event so
    # the frontend leaves its running state.
    try:
        outcome = await run_agent_goal_loop(
            session,
            entry_agent=entry_agent,
            initial_registry_key=registry_key,
            agent_factory_kwargs=agent_factory_kwargs,
            initial_input=pending.run_state,
            tool_context=tool_context,
            llm_model=llm_model,
            current_agent_name=current_agent_name,
        )
        await finalize_goal_outcome(session, outcome)
    except Exception as e:  # noqa: BLE001 - background task must never die silently
        logger.exception(
            "resume_after_max_turns_failed",
            session_id=session.session_id,
            current_agent=current_agent_name,
            error=str(e),
        )
        await publish_and_persist(
            session.session_id,
            system_warning(
                session.session_id,
                code="RESUME_FAILED",
                message=f"Failed to resume after max turns: {e}",
                suggestion="Please retry the request.",
            ),
        )
        await publish_and_persist(
            session.session_id,
            agent_finish(
                session.session_id,
                agent_name=current_agent_name,
                outcome="error",
                summary="Resume after max turns failed.",
            ),
        )
