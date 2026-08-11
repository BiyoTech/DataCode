"""Goal-run controller — outer loop for autonomous agent execution.

Wraps one or more SDK ``Runner.run_streamed`` segments so that *agent* style can
auto-resume after ``MaxTurnsExceeded`` or incomplete todos without waiting for
the user. *Interactive* style keeps the existing pause-and-wait behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import typing as t
from dataclasses import dataclass

from agents import Runner, RunConfig, RunState
from agents.exceptions import MaxTurnsExceeded
from agents.extensions.handoff_filters import remove_all_tools

from src.context.execution_style import ExecutionPolicy
from src.context.session import SessionContext, session_manager
from src.context.tool_context import ToolContext
from src.protocol.messages import agent_finish, message_complete, system_warning
from src.runtime.active_runs import active_chat_runs
from src.runtime.agent_resolver import registry_key_for_agent_name
from src.runtime.policy_resolver import (
    resolve_confirmation_threshold,
    resolve_default_tool_level,
    resolve_execution_policy,
    resolve_max_turns_for_run,
)
from src.runtime.streaming import (
    extract_new_run_items,
    handle_max_turns_exceeded,
    handle_turn_budget_exhausted,
    persist_new_run_items,
    resolve_segment_turns_used,
    stream_agent_run,
)
from src.runtime.timeline import publish_and_persist
from src.runtime.todo_board import (
    build_todo_continuation_nudge,
    has_incomplete_tasks,
    list_incomplete_tasks,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GoalRunController:
    """Tracks turn/resume/todo-continuation budget for a single user message goal."""

    policy: ExecutionPolicy
    total_turns_used: int = 0
    auto_resume_count: int = 0
    todo_continuation_count: int = 0
    # No-progress tracking: fingerprint of the previous continuation and the
    # number of CONSECUTIVE continuations that produced no forward progress.
    last_progress_fingerprint: t.Optional[str] = None
    stall_count: int = 0

    def turns_budget_for_next_run(self) -> int:
        remaining_total = max(0, self.policy.max_total_turns - self.total_turns_used)
        return max(0, min(self.policy.max_turns_per_run, remaining_total))

    def record_segment(self, turns: int) -> None:
        self.total_turns_used += max(0, turns)

    def can_auto_resume(self) -> bool:
        if not self.policy.auto_resume_on_max_turns:
            return False
        if self.auto_resume_count >= self.policy.max_auto_resumes:
            return False
        if self.total_turns_used >= self.policy.max_total_turns:
            return False
        return self.turns_budget_for_next_run() > 0

    def record_auto_resume(self) -> None:
        self.auto_resume_count += 1

    def can_todo_continue(self) -> bool:
        if not self.policy.require_todo_completion:
            return False
        if self.todo_continuation_count >= self.policy.max_todo_continuations:
            return False
        return self.turns_budget_for_next_run() > 0

    def record_todo_continuation(self) -> None:
        self.todo_continuation_count += 1

    def note_progress(self, fingerprint: str) -> bool:
        """Record a segment's progress fingerprint and report whether stalled.

        A "stall" means the current segment ended in exactly the same state as
        the previous continuation (same incomplete todos + same tool-call
        signature). Returns ``True`` once the number of consecutive stalls
        reaches ``policy.max_stall_continuations`` (0 disables the guard).
        """
        if self.policy.max_stall_continuations <= 0:
            return False
        if self.last_progress_fingerprint == fingerprint:
            self.stall_count += 1
        else:
            self.stall_count = 0
        self.last_progress_fingerprint = fingerprint
        return self.stall_count >= self.policy.max_stall_continuations


@dataclass
class AgentRunOutcome:
    """Result of ``run_agent_goal_loop``."""

    full_response: str = ""
    current_agent_name: str = "triage"
    approval_emitted: bool = False
    result: t.Any = None
    paused: bool = False
    cancelled: bool = False
    budget_exhausted: bool = False
    todos_incomplete: bool = False


async def _continue_for_incomplete_todos(
    session: SessionContext,
    *,
    result: t.Any,
    controller: GoalRunController,
    policy: ExecutionPolicy,
    outcome: AgentRunOutcome,
    agent_factory_kwargs: dict[str, t.Any],
    registry_fallback: str,
    from_max_turns: bool = False,
) -> tuple[bool, t.Any, t.Any, str]:
    """If todos remain, persist the segment and prepare the next loop iteration.

    Returns ``(should_continue, agent, run_input, registry_key)``.

    ``from_max_turns`` selects the resume strategy:
      - ``True``  (MaxTurnsExceeded): the agent was interrupted mid-work, so
        prefer ``result.to_state()`` to preserve in-flight tool calls and SDK
        context. No nudge is needed — the agent already knows what it was
        doing.
      - ``False`` (natural finish): the agent declared completion but todos
        remain. Build run_input from message history + an ephemeral system
        nudge (NOT persisted as a user message) to steer the agent back to
        the remaining items.
    """
    from src.agents.registry import AgentRegistry

    session_id = session.session_id
    if not (
        session.workspace_path
        and has_incomplete_tasks(session.workspace_path, session_id)
        and controller.can_todo_continue()
    ):
        return False, None, None, registry_fallback

    incomplete = list_incomplete_tasks(session.workspace_path, session_id)

    # No-progress guard: if this segment ended in the same state as the previous
    # continuation (same incomplete todos + same tool-call signature), the agent
    # is stuck in a failing loop. Stop instead of burning the whole continuation
    # budget, and surface a clear, actionable warning.
    fingerprint = _progress_fingerprint(result, incomplete)
    if controller.note_progress(fingerprint):
        await persist_new_run_items(session, result)
        logger.warning(
            "todo_continuation_stalled",
            session_id=session_id,
            continuation_count=controller.todo_continuation_count,
            stall_count=controller.stall_count,
            incomplete_tasks=len(incomplete),
        )
        await publish_and_persist(
            session_id,
            system_warning(
                session_id,
                code="NO_PROGRESS",
                message=(
                    f"Paused: no progress across {controller.stall_count + 1} "
                    f"consecutive attempts on {len(incomplete)} unfinished todo "
                    "item(s). The agent appears stuck (e.g. repeating a failing "
                    "action)."
                ),
                suggestion=(
                    "Review the remaining items and either give more specific "
                    "guidance or reply '继续' (continue) to retry."
                ),
            ),
        )
        outcome.todos_incomplete = True
        return False, None, None, registry_fallback

    await persist_new_run_items(session, result)
    controller.record_todo_continuation()

    registry_key = registry_key_for_agent_name(
        outcome.current_agent_name,
        fallback=registry_fallback,
    )
    agent = AgentRegistry.get(registry_key, **agent_factory_kwargs)
    run_input: t.Any

    if from_max_turns:
        # MaxTurnsExceeded: agent was interrupted mid-work. Preserve SDK state
        # (in-flight tool calls, current agent, reasoning) by resuming from
        # run_state. No nudge — the agent knows what it was doing.
        try:
            run_input = result.to_state()
            resumed_agent = getattr(run_input, "_current_agent", None)
            if resumed_agent is not None:
                agent = resumed_agent
            logger.info(
                "todo_continuation_from_run_state",
                session_id=session_id,
                continuation_count=controller.todo_continuation_count,
                incomplete_tasks=len(incomplete),
            )
        except Exception as e:
            logger.warning(
                "todo_continuation_run_state_failed_fallback_to_messages",
                session_id=session_id,
                error=str(e),
            )
            run_input = _build_nudged_run_input(session, incomplete)
    else:
        # Natural finish: agent said "I'm done" but todos remain.
        # Inject a system-role nudge (ephemeral — NOT persisted to the
        # messages table) so the LLM treats it as a platform directive
        # rather than a fake user instruction.
        run_input = _build_nudged_run_input(session, incomplete)

    logger.info(
        "todo_continuation_segment",
        session_id=session_id,
        continuation_count=controller.todo_continuation_count,
        incomplete_tasks=len(incomplete),
        agent_registry_key=registry_key,
        total_turns_used=controller.total_turns_used,
        from_max_turns=from_max_turns,
    )
    await publish_and_persist(
        session_id,
        system_warning(
            session_id,
            code="TODO_CONTINUATION",
            message=(
                f"Continuing — {len(incomplete)} todo item(s) still incomplete "
                f"({controller.todo_continuation_count}"
                f"/{policy.max_todo_continuations})."
            ),
            suggestion="",
        ),
    )
    return True, agent, run_input, registry_key


def _progress_fingerprint(result: t.Any, incomplete: list[dict[str, t.Any]]) -> str:
    """Build a stable signature of the agent's state after a segment.

    Combines the incomplete-todo snapshot with the tool calls issued in this
    segment. Two segments with an identical fingerprint mean the agent neither
    advanced its todo list nor did anything materially different — i.e. it is
    stuck. Tool outputs are intentionally excluded so transient error text does
    not mask a genuine no-progress loop.
    """
    parts: list[str] = [
        f"todo:{task.get('id')}:{task.get('status')}:{task.get('content')}"
        for task in incomplete
    ]
    for item in extract_new_run_items(result):
        if isinstance(item, dict):
            if item.get("type") == "function_call":
                parts.append(f"call:{item.get('name')}:{item.get('arguments')}")
        elif getattr(item, "type", None) == "function_call":
            name = getattr(item, "name", "")
            args = getattr(item, "arguments", "")
            parts.append(f"call:{name}:{args}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_nudged_run_input(
    session: SessionContext,
    incomplete: list[dict[str, t.Any]],
) -> list[dict[str, t.Any]]:
    """Build run_input from message history + an ephemeral system nudge.

    The nudge is NOT persisted to the messages table — it exists only in the
    returned list for this single segment. Using ``role="system"`` ensures the
    LLM treats it as a platform directive, not a fake user message.
    """
    messages = session_manager.get_messages(
        session.session_id, workspace_path=session.workspace_path
    )
    nudge = build_todo_continuation_nudge(incomplete)
    messages.append({"role": "system", "content": nudge})
    return messages


async def run_agent_goal_loop(
    session: SessionContext,
    *,
    entry_agent: t.Any,
    initial_registry_key: str,
    agent_factory_kwargs: dict[str, t.Any],
    initial_input: t.Any,
    tool_context: ToolContext,
    llm_model: t.Any,
    current_agent_name: str,
    seed_tool_calls: t.Optional[dict[str, dict[str, t.Any]]] = None,
) -> AgentRunOutcome:
    """Run the agent, auto-resuming across segments when policy allows."""
    session_id = session.session_id
    policy = resolve_execution_policy(session)
    controller = GoalRunController(policy=policy)

    run_input = initial_input
    agent = entry_agent
    outcome = AgentRunOutcome(current_agent_name=current_agent_name)
    registry_fallback = initial_registry_key
    # Tool-call metadata carried into the first segment only (approval resume).
    # Later segments build their own cache from freshly streamed tool calls.
    segment_seed = seed_tool_calls

    while True:
        max_turns = resolve_max_turns_for_run(session, total_turns_used=controller.total_turns_used)
        segment_budget = controller.turns_budget_for_next_run()
        if segment_budget:
            max_turns = min(max_turns, segment_budget)

        if max_turns <= 0:
            await handle_turn_budget_exhausted(session, outcome.current_agent_name, policy)
            outcome.budget_exhausted = True
            outcome.paused = True
            return outcome

        # When resuming from a RunState (e.g. after a human approval), the state
        # already carries its own context wrapper that holds the recorded
        # approval decisions. Passing a fresh ``context`` here would make the SDK
        # overwrite ``run_state._context`` (see ``resolve_resumed_context``),
        # wiping those approvals — the approved tool would then be treated as
        # un-approved and never execute, leaving the UI tool card stuck. So we
        # only pass ``context`` for fresh (non-resumed) segments.
        is_resumed_state = isinstance(run_input, RunState)
        result = Runner.run_streamed(
            agent,
            input=run_input,
            context=None if is_resumed_state else tool_context,
            max_turns=max_turns,
            run_config=RunConfig(model=llm_model, handoff_input_filter=remove_all_tools),
        )
        active_chat_runs[session_id] = {
            "task": asyncio.current_task(),
            "result": result,
            "agent_name": outcome.current_agent_name,
        }

        try:
            full_response, agent_name, approval_emitted = await stream_agent_run(
                session,
                result,
                outcome.current_agent_name,
                tool_context,
                seed_tool_calls=segment_seed,
            )
            # Seed only applies to the first (resume) segment.
            segment_seed = None
            outcome.full_response = full_response
            outcome.current_agent_name = agent_name
            outcome.approval_emitted = approval_emitted
            outcome.result = result
            controller.record_segment(resolve_segment_turns_used(result, max_turns))

            if approval_emitted:
                return outcome

            continued, next_agent, next_input, registry_fallback = (
                await _continue_for_incomplete_todos(
                    session,
                    result=result,
                    controller=controller,
                    policy=policy,
                    outcome=outcome,
                    agent_factory_kwargs=agent_factory_kwargs,
                    registry_fallback=registry_fallback,
                    from_max_turns=False,
                )
            )
            if continued:
                # Seal this segment's trailing assistant text as a
                # MESSAGE_COMPLETE timeline event before continuing. Otherwise
                # the live streaming bubble is never finalized (stays
                # half-rendered) and only gets "closed" if the next segment
                # happens to restate the same text — which is exactly what
                # produced the duplicated paragraphs. This event is timeline-only
                # (LLM replay history is persisted separately via run items), so
                # it does not double the conversation history.
                if outcome.full_response:
                    await publish_and_persist(
                        session_id,
                        message_complete(
                            session_id,
                            agent_name=outcome.current_agent_name,
                            content=outcome.full_response,
                        ),
                    )
                    outcome.full_response = ""
                agent = next_agent
                run_input = next_input
                continue

            # Not continuing but todos remain (continuation budget exhausted or
            # policy disallows it): mark partial so the finalizer reports it.
            if (
                session.workspace_path
                and has_incomplete_tasks(session.workspace_path, session.session_id)
                and policy.require_todo_completion
            ):
                outcome.todos_incomplete = True

            return outcome

        except asyncio.CancelledError:
            logger.info("agent_run_cancelled", session_id=session_id)
            await publish_and_persist(
                session_id,
                agent_finish(session_id, agent_name=outcome.current_agent_name, outcome="cancelled"),
            )
            outcome.cancelled = True
            return outcome

        except MaxTurnsExceeded:
            await persist_new_run_items(session, result)
            controller.record_segment(resolve_segment_turns_used(result, max_turns))

            continued, next_agent, next_input, registry_fallback = (
                await _continue_for_incomplete_todos(
                    session,
                    result=result,
                    controller=controller,
                    policy=policy,
                    outcome=outcome,
                    agent_factory_kwargs=agent_factory_kwargs,
                    registry_fallback=registry_fallback,
                    from_max_turns=True,
                )
            )
            if continued:
                agent = next_agent
                run_input = next_input
                continue

            if not controller.can_auto_resume():
                await handle_max_turns_exceeded(
                    session,
                    result,
                    outcome.current_agent_name,
                    policy=policy,
                    auto_resume_attempted=controller.auto_resume_count > 0,
                )
                outcome.paused = True
                outcome.result = result
                return outcome

            try:
                run_state = result.to_state()
            except Exception as e:
                logger.warning(
                    "auto_resume_to_state_failed",
                    session_id=session_id,
                    error=str(e),
                )
                await handle_max_turns_exceeded(
                    session,
                    result,
                    outcome.current_agent_name,
                    policy=policy,
                    auto_resume_attempted=False,
                )
                outcome.paused = True
                outcome.result = result
                return outcome

            controller.record_auto_resume()
            run_input = run_state
            agent = getattr(run_state, "_current_agent", None) or agent
            outcome.current_agent_name = (
                getattr(agent, "name", outcome.current_agent_name) or outcome.current_agent_name
            )

            logger.info(
                "auto_resume_segment",
                session_id=session_id,
                resume_count=controller.auto_resume_count,
                total_turns_used=controller.total_turns_used,
                max_auto_resumes=policy.max_auto_resumes,
            )
            await publish_and_persist(
                session_id,
                system_warning(
                    session_id,
                    code="AUTO_RESUME",
                    message=(
                        f"Continuing autonomously (segment {controller.auto_resume_count}"
                        f"/{policy.max_auto_resumes})…"
                    ),
                    suggestion="",
                ),
            )
            continue

        finally:
            active_chat_runs.pop(session_id, None)


async def finalize_goal_outcome(session: SessionContext, outcome: AgentRunOutcome) -> None:
    """Publish messages and agent_finish after a goal loop completes."""
    if outcome.cancelled or outcome.paused:
        return

    if outcome.result is not None:
        await persist_new_run_items(session, outcome.result)

    if outcome.full_response:
        await publish_and_persist(
            session.session_id,
            message_complete(
                session.session_id,
                agent_name=outcome.current_agent_name,
                content=outcome.full_response,
            ),
        )
    elif outcome.result is not None and outcome.result.final_output:
        await publish_and_persist(
            session.session_id,
            message_complete(
                session.session_id,
                agent_name=outcome.current_agent_name,
                content=outcome.result.final_output,
            ),
        )

    if not outcome.approval_emitted:
        finish_outcome = "success"
        finish_summary = ""
        if outcome.todos_incomplete:
            finish_outcome = "partial"
            finish_summary = "Finished with incomplete todos remaining."
        await publish_and_persist(
            session.session_id,
            agent_finish(
                session.session_id,
                agent_name=outcome.current_agent_name,
                outcome=finish_outcome,
                summary=finish_summary,
            ),
        )


def build_agent_factory_kwargs(
    session: SessionContext,
    *,
    agent_mode: str,
    composed_memory: str,
    llm_model: t.Any,
    execution_style: str,
) -> dict[str, t.Any]:
    workspace_ctx = session.workspace_context
    return {
        "workspace_path": session.workspace_path,
        "mode": agent_mode,
        "rules": workspace_ctx.rules if workspace_ctx else [],
        "memory_index": composed_memory,
        "environment": session.environment,
        "model": llm_model,
        "user_id": session.user_id,
        "session_id": session.session_id,
        "execution_style": execution_style,
    }


def initial_registry_key_for_session(session: SessionContext) -> str:
    return "data_analysis_agent" if session.scenario == "chatbi" else "triage"


def build_tool_context(session: SessionContext) -> ToolContext:
    """Construct a :class:`ToolContext` with policy-resolved thresholds."""
    return ToolContext(
        workspace_path=session.workspace_path,
        user_id=session.user_id,
        session_id=session.session_id,
        mode=session.mode.value,
        execution_style=session.execution_style.value,
        default_tool_level=resolve_default_tool_level(session),
        require_confirmation_above=resolve_confirmation_threshold(session),
    )
