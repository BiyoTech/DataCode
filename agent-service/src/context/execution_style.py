"""Execution Style — how autonomously the agent runs.

This is a SECOND dimension, orthogonal to ``InteractionMode`` (ask/craft/design):

  - ``InteractionMode`` controls WHAT the agent may do (capability / permissions).
  - ``ExecutionStyle`` controls HOW it runs (autonomy / interaction pattern).

The default is ``AGENT``: the agent runs autonomously through its ReAct loop until
the user's goal is achieved, without pausing to ask "shall I continue?" between
steps. ``INTERACTIVE`` preserves the step-by-step, confirm-before-proceeding
behaviour.

Design note — extensibility:
    Styles are backed by a registry of :class:`ExecutionPolicy` objects. A new
    style (e.g. ``supervised`` or ``batch``) can be added by registering another
    policy here, WITHOUT touching the executor, agent factory, or prompt files.
    Runtime knobs (turn budget, approval threshold, auto-resume) live on the
    policy so that behaviour is data-driven rather than scattered across prompts
    and ``if style == ...`` branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionStyle(str, Enum):
    """How autonomously the agent executes a user request."""

    AGENT = "agent"              # Autonomous: run to completion (default)
    INTERACTIVE = "interactive"  # Step-by-step: confirm before proceeding
    # Reserved for future expansion (register policies below when implemented):
    # SUPERVISED = "supervised"  # Auto for low-risk, confirm for key steps
    # BATCH = "batch"            # Plan first, then execute the whole plan

    @classmethod
    def from_value(
        cls, value: "str | ExecutionStyle | None", default: "ExecutionStyle | None" = None
    ) -> "ExecutionStyle":
        """Coerce a raw value into an ExecutionStyle, falling back to *default*.

        Unknown / empty values resolve to *default* (or the global DEFAULT_STYLE)
        so a bad client payload never breaks a chat run.
        """
        fallback = default or DEFAULT_STYLE
        if isinstance(value, cls):
            return value
        if not value:
            return fallback
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return fallback


@dataclass(frozen=True)
class ExecutionPolicy:
    """Resolved behaviour bundle for a single execution style.

    ``instructions`` is injected into the system prompt. The remaining knobs are
    consumed by the runtime (policy resolver / goal-run controller / approval
    layer).
    """

    style: ExecutionStyle
    instructions: str
    # Turn budget (per-run and per-message ceilings).
    max_turns_per_run: int
    max_auto_resumes: int
    max_total_turns: int
    # Approval: tools with level >= this require user confirmation.
    require_confirmation_above: int
    # Loop control.
    auto_resume_on_max_turns: bool
    require_todo_completion: bool
    max_todo_continuations: int
    # No-progress guard: stop todo-continuation after this many CONSECUTIVE
    # segments that produced no forward progress (identical incomplete todo set
    # AND identical tool-call signature). 0 disables the guard. This prevents
    # an agent stuck in a failing loop (e.g. repeatedly re-planning the same
    # broken SQL) from burning the full continuation budget.
    max_stall_continuations: int
    forbid_confirmation_questions: bool


DEFAULT_STYLE = ExecutionStyle.AGENT


_AGENT_INSTRUCTIONS = (
    "**Execution Style: Agent (Autonomous)**\n"
    "This block governs your interaction rhythm and OVERRIDES any general 'clarify "
    "first' / 'confirm before proceeding' guidance elsewhere in this prompt.\n"
    "- Work autonomously until the user's goal is fully achieved. Keep going across "
    "steps instead of stopping to report partial progress.\n"
    "- Do NOT ask permission to continue (e.g. \"shall I proceed?\", \"是否继续下一步?\"). "
    "Only pause when a genuinely blocking decision needs the user, or a high-risk "
    "action requires the platform's approval.\n"
    "- Clarify ONLY when intent is genuinely ambiguous AND it blocks ALL progress. "
    "For a clear multi-step task, do NOT ask \"which approach do you prefer?\" — pick "
    "a reasonable default, briefly state the assumption you made, and proceed.\n"
    "- If information is missing, first try to obtain it yourself with your tools "
    "(schema discovery, reading models/ontology, search) before asking the user.\n"
    "- Only ask the user when a REQUIRED input is missing AND cannot be inferred or "
    "discovered, and it blocks all further progress. Ask one concise question, then "
    "continue once answered.\n"
    "- For multi-step work, call `write_todo` first to lay out the plan, then execute "
    "every item and keep the list updated until all are complete.\n"
    "- Finish with a concise completion summary (what you did + key results), not a "
    "question asking whether to continue."
)

_INTERACTIVE_INSTRUCTIONS = (
    "**Execution Style: Interactive (Step-by-Step)**\n"
    "- Collaborate with the user step by step. Break complex work into clear stages.\n"
    "- Briefly explain your plan before making changes, and confirm with the user "
    "before high-impact or irreversible actions.\n"
    "- Ask a clarifying question whenever the intent or a key parameter is ambiguous.\n"
    "- Pause after each major milestone so the user can review and steer.\n"
    "- Prefer smaller, reviewable increments over doing everything at once."
)


_POLICIES: "dict[ExecutionStyle, ExecutionPolicy]" = {
    ExecutionStyle.AGENT: ExecutionPolicy(
        style=ExecutionStyle.AGENT,
        instructions=_AGENT_INSTRUCTIONS,
        max_turns_per_run=30,
        max_auto_resumes=5,
        max_total_turns=120,
        require_confirmation_above=4,   # only L4+ (apply/deploy/admin) needs approval
        auto_resume_on_max_turns=True,
        require_todo_completion=True,
        max_todo_continuations=4,
        max_stall_continuations=2,
        forbid_confirmation_questions=True,
    ),
    ExecutionStyle.INTERACTIVE: ExecutionPolicy(
        style=ExecutionStyle.INTERACTIVE,
        instructions=_INTERACTIVE_INSTRUCTIONS,
        max_turns_per_run=20,
        max_auto_resumes=0,
        max_total_turns=20,
        require_confirmation_above=3,   # current behaviour (L3+ needs approval)
        auto_resume_on_max_turns=False,
        require_todo_completion=False,
        max_todo_continuations=0,
        max_stall_continuations=0,
        forbid_confirmation_questions=False,
    ),
}


def get_policy(style: "str | ExecutionStyle | None") -> ExecutionPolicy:
    """Return the :class:`ExecutionPolicy` for *style* (falls back to default)."""
    resolved = ExecutionStyle.from_value(style)
    return _POLICIES[resolved]


def get_instructions(style: "str | ExecutionStyle | None") -> str:
    """Return the system-prompt instruction block for *style*."""
    return get_policy(style).instructions


def resolve_execution_overlay(
    overlay: "str | dict | None",
    style: "str | ExecutionStyle | None",
) -> str:
    """Resolve a per-agent execution overlay for the active *style*.

    ``overlay`` comes from an agent's YAML ``execution_overlay`` field and may be:
      - ``str``  : a single overlay applied to every style.
      - ``dict`` : keyed by style value ('agent' / 'interactive'); the matching
                   entry is used, else an optional ``"all"`` fallback.
    Returns an empty string when there is no applicable overlay.
    """
    if not overlay:
        return ""
    if isinstance(overlay, str):
        return overlay.strip()
    if isinstance(overlay, dict):
        resolved = ExecutionStyle.from_value(style)
        value = overlay.get(resolved.value)
        if value is None:
            value = overlay.get("all")
        return str(value).strip() if value else ""
    return ""


def compose_execution_instructions(
    style: "str | ExecutionStyle | None",
    overlay: "str | dict | None" = None,
) -> str:
    """Base style instructions + an optional per-agent overlay block."""
    base = get_instructions(style)
    extra = resolve_execution_overlay(overlay, style)
    if extra:
        return f"{base}\n\n{extra}"
    return base
