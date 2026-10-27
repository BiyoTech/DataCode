"""Command-level approval coordinator for shell command security policy.

When a shell command batch triggers a 'confirm' action, this module:
  1. Publishes a human_required SSE event to the frontend via the event bus.
  2. Registers an asyncio.Future and blocks (with timeout) until the user responds.
  3. On resolution (POST /approve), the future is completed and the shell tool
     continues execution.

Important design distinction from SDK-level ToolApprovalItem:
  - SDK approval pauses the entire run (Runner.run_streamed ends, needs resume).
  - Command-level approval blocks inside _on_invoke via asyncio.Future while
    the tool is still running — no SDK resume needed.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Dict, List, Optional

from src.context.event_bus import event_bus
from src.protocol.messages import human_required
from src.tools.command_policy import PolicyDecision
from src.tools.registry import risk_badge_for_level
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Default timeout for command approval (seconds)
DEFAULT_APPROVAL_TIMEOUT_S = 300.0


class CommandApprovalRequest:
    """Pending command-level approval request with a settable future."""

    def __init__(self, session_id: str, request_id: str, commands: List[str]) -> None:
        self.session_id = session_id
        self.request_id = request_id
        self.commands = commands
        # Lazy-init: see PendingApproval in runtime/approval.py for rationale.
        # The future is bound to the running event loop on first access.
        self._future: Optional[asyncio.Future[bool]] = None

    @property
    def future(self) -> asyncio.Future[bool]:
        """Return the future, creating it lazily on the running event loop."""
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future


# In-memory registry: {session_id: {request_id: CommandApprovalRequest}}
_registry: Dict[str, Dict[str, CommandApprovalRequest]] = {}


def _generate_request_id() -> str:
    """Generate a stable, unique request ID for command approval events."""
    return f"cmd-approval-{uuid.uuid4().hex[:12]}"


def _register_remote(session_id: str, request_id: str) -> None:
    """Cross-replica existence for this pending command approval (best-effort)."""
    import asyncio

    from src.runtime.session_coordination import (
        KIND_COMMAND_APPROVAL,
        get_session_coordination,
    )

    coro = get_session_coordination().register_pending(
        KIND_COMMAND_APPROVAL, session_id, request_id, int(DEFAULT_APPROVAL_TIMEOUT_S)
    )
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def _unregister_remote(session_id: str, request_id: str) -> None:
    import asyncio

    from src.runtime.session_coordination import (
        KIND_COMMAND_APPROVAL,
        get_session_coordination,
    )

    coro = get_session_coordination().unregister_pending(
        KIND_COMMAND_APPROVAL, session_id, request_id
    )
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


async def request_command_approval(
    session_id: str,
    commands: List[str],
    decision: PolicyDecision,
    timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
) -> bool:
    """Request human approval for a batch of shell commands.

    Publishes a human_required SSE event, registers a future, and blocks
    until the user responds or the timeout expires.

    Args:
        session_id: Session identifier.
        commands: The batch of commands requiring approval.
        decision: PolicyDecision (must have action='confirm').
        timeout_s: Maximum seconds to wait for user response.

    Returns:
        True if approved, False if rejected or timed out.
    """
    request_id = _generate_request_id()

    # Resolve risk badge metadata — use the single source of truth in
    # ToolRegistry so badge tones stay consistent across the whole service.
    risk_label, risk_tone = risk_badge_for_level(decision.risk_level)

    # Register the pending request with a future
    approval = CommandApprovalRequest(session_id, request_id, commands)
    session_registry = _registry.setdefault(session_id, {})
    session_registry[request_id] = approval
    _register_remote(session_id, request_id)

    logger.info(
        "command_approval_requested",
        session_id=session_id,
        request_id=request_id,
        command_count=len(commands),
        matched_rule=decision.matched_rule,
        risk_level=decision.risk_level,
    )

    # Publish human_required SSE event for the frontend approval card
    event = human_required(
        session_id=session_id,
        request_id=request_id,
        request_type="command_approval",
        title="Shell Command Approval Required",
        description=f"Rule: {decision.matched_rule}",
        tool_name="shell",
        tool_level=decision.risk_level,
        risk_badge_label=risk_label,
        risk_badge_tone=risk_tone,
        commands=commands,
        rule_name=decision.matched_rule,
        reason=decision.reason,
    )
    await event_bus.publish(session_id, event)

    # Block on the future with timeout
    try:
        approved = await asyncio.wait_for(approval.future, timeout=timeout_s)
        logger.info(
            "command_approval_resolved",
            session_id=session_id,
            request_id=request_id,
            approved=approved,
        )
        return approved
    except asyncio.TimeoutError:
        logger.warning(
            "command_approval_timeout",
            session_id=session_id,
            request_id=request_id,
            timeout_s=timeout_s,
        )
        _cleanup_request(session_id, request_id)
        return False


def resolve_command_approval(
    session_id: str,
    request_id: str,
    approved: bool,
) -> bool:
    """Resolve a pending command-level approval.

    Called from main.py's post_approval endpoint when the frontend sends
    an approve/reject response.

    Args:
        session_id: Session identifier.
        request_id: The approval request ID.
        approved: True if user approved, False if rejected.

    Returns:
        True if the request was found and resolved, False otherwise.
    """
    session_registry = _registry.get(session_id, {})
    approval = session_registry.get(request_id)
    if approval is None:
        logger.warning(
            "command_approval_not_found",
            session_id=session_id,
            request_id=request_id,
        )
        return False

    if not approval.future.done():
        approval.future.set_result(approved)

    _cleanup_request(session_id, request_id)
    _unregister_remote(session_id, request_id)

    logger.info(
        "command_approval_resolved_by_user",
        session_id=session_id,
        request_id=request_id,
        approved=approved,
    )
    return True


def _cleanup_request(session_id: str, request_id: str) -> None:
    """Remove a resolved/timed-out request from the registry."""
    session_registry = _registry.get(session_id, {})
    if request_id in session_registry:
        del session_registry[request_id]
    if not session_registry:
        _registry.pop(session_id, None)
