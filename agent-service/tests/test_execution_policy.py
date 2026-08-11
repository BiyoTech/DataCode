"""Tests for execution-style policy resolution and goal-run controller."""

from __future__ import annotations

import pytest

from src.context.execution_style import ExecutionStyle, get_policy
from src.context.session import SessionContext, WorkspaceContext
from src.runtime.goal_run import GoalRunController
from src.runtime.policy_resolver import (
    resolve_confirmation_threshold,
    resolve_execution_policy,
    resolve_max_turns_for_run,
)


def _session(
    style: ExecutionStyle = ExecutionStyle.AGENT,
    workspace_tools: dict | None = None,
) -> SessionContext:
    config: dict = {}
    if workspace_tools is not None:
        config = {"agent": {"tools": workspace_tools}}
    return SessionContext(
        session_id="sess-1",
        user_id="user-1",
        execution_style=style,
        workspace_context=WorkspaceContext(
            workspace_path="/w",
            config=config,
        ),
    )


def test_agent_style_confirmation_threshold_defaults_to_four():
    session = _session(ExecutionStyle.AGENT)
    assert resolve_confirmation_threshold(session) == 4


def test_interactive_style_confirmation_threshold_defaults_to_three():
    session = _session(ExecutionStyle.INTERACTIVE)
    assert resolve_confirmation_threshold(session) == 3


def test_workspace_can_tighten_confirmation_threshold():
    session = _session(
        ExecutionStyle.AGENT,
        workspace_tools={"require_confirmation_above": 2},
    )
    # Lower threshold = more approvals required.
    assert resolve_confirmation_threshold(session) == 2


def test_workspace_cannot_loosen_below_policy():
    session = _session(
        ExecutionStyle.INTERACTIVE,
        workspace_tools={"require_confirmation_above": 5},
    )
    # Interactive policy baseline is 3; workspace 5 would loosen, so policy wins.
    assert resolve_confirmation_threshold(session) == 3


def test_resolve_execution_policy_from_session():
    session = _session(ExecutionStyle.AGENT)
    policy = resolve_execution_policy(session)
    assert policy.style == ExecutionStyle.AGENT
    assert policy.auto_resume_on_max_turns is True


def test_resolve_max_turns_for_run_uses_policy_and_settings(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "max_turns_per_message", 50)
    session = _session(ExecutionStyle.AGENT)
    assert resolve_max_turns_for_run(session) == 30  # policy.max_turns_per_run

    assert resolve_max_turns_for_run(session, total_turns_used=110) == 10
    assert resolve_max_turns_for_run(session, total_turns_used=120) == 0


def test_goal_run_controller_auto_resume_budget():
    policy = get_policy(ExecutionStyle.AGENT)
    controller = GoalRunController(policy=policy)

    assert controller.can_auto_resume() is True
    controller.record_segment(30)
    controller.record_auto_resume()
    assert controller.can_auto_resume() is True

    for _ in range(policy.max_auto_resumes - 1):
        controller.record_auto_resume()
    assert controller.can_auto_resume() is False


def test_goal_run_controller_interactive_never_auto_resumes():
    policy = get_policy(ExecutionStyle.INTERACTIVE)
    controller = GoalRunController(policy=policy)
    assert controller.can_auto_resume() is False
    assert controller.can_todo_continue() is False


def test_goal_run_controller_todo_continuation_budget():
    policy = get_policy(ExecutionStyle.AGENT)
    controller = GoalRunController(policy=policy)
    assert controller.can_todo_continue() is True
    for _ in range(policy.max_todo_continuations):
        controller.record_todo_continuation()
    assert controller.can_todo_continue() is False
    assert policy.max_todo_continuations == 4


def test_goal_run_controller_stops_on_no_progress():
    """Repeated identical segments trip the no-progress guard before the cap."""
    policy = get_policy(ExecutionStyle.AGENT)
    controller = GoalRunController(policy=policy)
    fingerprint = "same-state"
    stalled = False
    for _ in range(policy.max_stall_continuations + 1):
        stalled = controller.note_progress(fingerprint)
    assert stalled is True
    assert policy.max_stall_continuations == 2


def test_resolve_execution_overlay_string_applies_to_all_styles():
    from src.context.execution_style import resolve_execution_overlay

    assert resolve_execution_overlay("  hi  ", "agent") == "hi"
    assert resolve_execution_overlay("hi", "interactive") == "hi"
    assert resolve_execution_overlay(None, "agent") == ""
    assert resolve_execution_overlay("", "agent") == ""


def test_resolve_execution_overlay_dict_is_style_scoped():
    from src.context.execution_style import resolve_execution_overlay

    overlay = {"agent": "AUTO", "interactive": "STEP"}
    assert resolve_execution_overlay(overlay, "agent") == "AUTO"
    assert resolve_execution_overlay(overlay, "interactive") == "STEP"
    # unknown style falls back to DEFAULT_STYLE (agent) then optional 'all'
    assert resolve_execution_overlay({"all": "SHARED"}, "interactive") == "SHARED"
    assert resolve_execution_overlay({"agent": "AUTO"}, "interactive") == ""


def test_compose_execution_instructions_appends_overlay():
    from src.context.execution_style import (
        compose_execution_instructions,
        get_instructions,
    )

    base = get_instructions("agent")
    composed = compose_execution_instructions("agent", {"agent": "EXTRA"})
    assert composed.startswith(base)
    assert composed.endswith("EXTRA")
    # no overlay -> identical to base
    assert compose_execution_instructions("agent", None) == base
