"""SDK tool approval registry for paused agent runs."""

import asyncio
import time
import typing as t

from agents.items import ToolApprovalItem

from src.utils.logging import get_logger

logger = get_logger(__name__)

# TTL for a pending approval entry. If the user never responds (e.g. closes the
# browser), the entry is auto-cleaned after this many seconds so the registry
# does not grow unbounded and the bound future can be released.
_PENDING_APPROVAL_TTL_SECONDS = 10 * 60  # 10 minutes


class PendingApproval:
    """Represents a pending human approval request."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        tool_name: str,
        description: str,
        run_state: t.Any,
        approval_item: ToolApprovalItem,
    ):
        self.request_id = request_id
        self.session_id = session_id
        self.tool_name = tool_name
        self.description = description
        self.run_state = run_state
        self.approval_item = approval_item
        # UTC epoch seconds when the entry was registered, used for TTL cleanup.
        self.created_at: float = time.time()
        # Lazy-init: a Future must be bound to the running event loop.
        # asyncio.get_event_loop() is deprecated in Python 3.10+ and raises
        # when no loop is running (e.g. constructed in a sync context).
        # The future is created on first access via get_running_loop() instead.
        self._future: t.Optional[asyncio.Future[bool]] = None

    @property
    def future(self) -> asyncio.Future[bool]:
        """Return the future, creating it lazily on the running event loop."""
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    def is_expired(self, now: t.Optional[float] = None) -> bool:
        """Return True if this entry has exceeded its TTL."""
        return (now or time.time()) - self.created_at > _PENDING_APPROVAL_TTL_SECONDS


_pending_approvals: t.Dict[str, t.Dict[str, PendingApproval]] = {}


def register_pending_approval(approval: PendingApproval) -> None:
    session_approvals = _pending_approvals.setdefault(approval.session_id, {})
    session_approvals[approval.request_id] = approval
    logger.info(
        "approval_registered",
        session_id=approval.session_id,
        request_id=approval.request_id,
        tool_name=approval.tool_name,
    )


def resolve_pending_approval(session_id: str, request_id: str, approved: bool) -> bool:
    session_approvals = _pending_approvals.get(session_id, {})
    approval = session_approvals.pop(request_id, None)
    if not approval:
        logger.warning("approval_not_found", session_id=session_id, request_id=request_id)
        return False

    if not approval.future.done():
        approval.future.set_result(approved)

    logger.info(
        "approval_resolved",
        session_id=session_id,
        request_id=request_id,
        approved=approved,
    )
    return True


def get_pending_approval(session_id: str, request_id: str) -> t.Optional[PendingApproval]:
    return _pending_approvals.get(session_id, {}).get(request_id)


def cleanup_expired_approvals(now: t.Optional[float] = None) -> int:
    """Drop pending approval entries that have outlived their TTL.

    Returns the number of entries removed. Safe to call from any async or sync
    context; intended to be invoked on session load / periodic tick.
    """
    now = now or time.time()
    removed = 0
    for session_id, session_approvals in list(_pending_approvals.items()):
        for request_id, approval in list(session_approvals.items()):
            if approval.is_expired(now):
                # Resolve the future as rejected so any awaiting coroutine
                # unblocks rather than hanging forever.
                if not approval.future.done():
                    approval.future.set_result(False)
                session_approvals.pop(request_id, None)
                removed += 1
                logger.info(
                    "approval_expired",
                    session_id=session_id,
                    request_id=request_id,
                    tool_name=approval.tool_name,
                )
        if not session_approvals:
            _pending_approvals.pop(session_id, None)
    return removed
