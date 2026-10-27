"""Max-turns resume registry and resume-intent detection."""

import time
import typing as t

from src.utils.logging import get_logger

logger = get_logger(__name__)

_RESUME_INTENT_KEYWORDS = {"继续", "繼續", "continue", "go on", "keep going", "resume"}

# TTL for a pending resume entry. If the user never replies with a resume
# intent, the entry is auto-cleaned after this many seconds so the registry
# does not grow unbounded.
_PENDING_RESUME_TTL_SECONDS = 30 * 60  # 30 minutes


class PendingResume:
    """Run interrupted by max_turns that can be resumed from saved state."""

    def __init__(self, session_id: str, run_state: t.Any, current_agent_name: str):
        self.session_id = session_id
        self.run_state = run_state
        self.current_agent_name = current_agent_name
        # UTC epoch seconds when the entry was registered, used for TTL cleanup.
        self.created_at: float = time.time()

    def is_expired(self, now: t.Optional[float] = None) -> bool:
        """Return True if this entry has exceeded its TTL."""
        return (now or time.time()) - self.created_at > _PENDING_RESUME_TTL_SECONDS


_pending_resumes: t.Dict[str, PendingResume] = {}


def _register_remote(session_id: str) -> None:
    """Make the paused run's existence visible to every replica (best-effort).

    The TTL mirrors the local entry so a replica crash self-heals both sides.
    """
    import asyncio

    from src.runtime.session_coordination import KIND_RESUME, get_session_coordination

    coro = get_session_coordination().register_pending(
        KIND_RESUME, session_id, "", _PENDING_RESUME_TTL_SECONDS
    )
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def _unregister_remote(session_id: str) -> None:
    import asyncio

    from src.runtime.session_coordination import KIND_RESUME, get_session_coordination

    coro = get_session_coordination().unregister_pending(KIND_RESUME, session_id, "")
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def register_pending_resume(resume: PendingResume) -> None:
    _pending_resumes[resume.session_id] = resume
    _register_remote(resume.session_id)
    logger.info(
        "resume_registered",
        session_id=resume.session_id,
        current_agent=resume.current_agent_name,
    )


def pop_pending_resume(session_id: str) -> t.Optional[PendingResume]:
    popped = _pending_resumes.pop(session_id, None)
    if popped is not None:
        _unregister_remote(session_id)
    return popped


def peek_pending_resume(session_id: str) -> t.Optional[PendingResume]:
    """Read without consuming: chat routing checks BEFORE deciding to forward
    a resume request to the replica that owns the run state."""
    return _pending_resumes.get(session_id)


def is_resume_intent(content: str) -> bool:
    normalized = (content or "").strip().lower().rstrip("。.!！~ ")
    return normalized in _RESUME_INTENT_KEYWORDS


def cleanup_expired_resumes(now: t.Optional[float] = None) -> int:
    """Drop pending resume entries that have outlived their TTL.

    Returns the number of entries removed. Intended to be invoked on session
    load / periodic tick so the registry does not accumulate entries for users
    who abandoned the session mid-pause.
    """
    now = now or time.time()
    removed = 0
    for session_id, resume in list(_pending_resumes.items()):
        if resume.is_expired(now):
            _pending_resumes.pop(session_id, None)
            removed += 1
            logger.info(
                "resume_expired",
                session_id=session_id,
                current_agent=resume.current_agent_name,
            )
    return removed
