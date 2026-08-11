"""Resolve runtime knobs from ExecutionPolicy + workspace overrides.

Precedence for ``require_confirmation_above``:
  1. Start from the execution-style policy (agent=4, interactive=3).
  2. If the workspace sets an explicit threshold, take the *more restrictive*
     value (lower number → more tools require approval).

Turn budget for a single Runner segment:
  ``min(policy.max_turns_per_run, policy.max_total_turns - used, settings.max_turns_per_message)``
"""

from __future__ import annotations

import typing as t

from src.config import settings
from src.context.execution_style import ExecutionPolicy, get_policy
from src.context.session import SessionContext

if t.TYPE_CHECKING:
    pass


def _workspace_agent_cfg(session: SessionContext) -> dict:
    workspace_config = (session.workspace_context.config if session.workspace_context else {}) or {}
    if not isinstance(workspace_config, dict):
        return {}
    agent_cfg = workspace_config.get("agent", {})
    return agent_cfg if isinstance(agent_cfg, dict) else {}


def _workspace_tools_cfg(session: SessionContext) -> dict:
    tools_cfg = _workspace_agent_cfg(session).get("tools", {})
    return tools_cfg if isinstance(tools_cfg, dict) else {}


def resolve_execution_policy(session: SessionContext) -> ExecutionPolicy:
    """Return the effective :class:`ExecutionPolicy` for *session*."""
    return get_policy(session.execution_style)


def resolve_confirmation_threshold(session: SessionContext) -> int:
    """Approval threshold: policy baseline, workspace may tighten only."""
    policy = resolve_execution_policy(session)
    base = policy.require_confirmation_above
    ws_raw = _workspace_tools_cfg(session).get("require_confirmation_above")
    if ws_raw is None:
        return base
    try:
        ws_threshold = int(ws_raw)
    except (TypeError, ValueError):
        return base
    # Lower threshold → more tools need approval → more restrictive.
    return min(base, ws_threshold)


def resolve_default_tool_level(session: SessionContext) -> int:
    """Default tool risk level from workspace config (unchanged by style)."""
    from src.tools.registry import DEFAULT_TOOL_LEVEL

    ws_raw = _workspace_tools_cfg(session).get("default_level")
    if ws_raw is None:
        return DEFAULT_TOOL_LEVEL
    try:
        return int(ws_raw)
    except (TypeError, ValueError):
        return DEFAULT_TOOL_LEVEL


def resolve_max_turns_for_run(session: SessionContext, *, total_turns_used: int = 0) -> int:
    """Turn budget for the next Runner segment."""
    policy = resolve_execution_policy(session)
    remaining_total = max(0, policy.max_total_turns - total_turns_used)
    per_message = settings.max_turns_per_message
    ws_raw = _workspace_agent_cfg(session).get("max_turns_per_message")
    if ws_raw is not None:
        try:
            per_message = int(ws_raw)
        except (TypeError, ValueError):
            pass
    return max(0, min(policy.max_turns_per_run, remaining_total, per_message))
