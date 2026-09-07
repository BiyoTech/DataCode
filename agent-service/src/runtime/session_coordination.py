"""Cross-replica coordination for agent-service session state.

## What is still process-local, and why that hurts

Three pieces of session state cannot be serialized into Redis because they are
live asyncio objects bound to this process's event loop:

1. **Pending approvals** (SDK ``ToolApprovalItem`` futures and command-level
   approval futures). A POST /approve that lands on a replica other than the
   one running the agent finds an empty registry and 404s — the user clicks
   "approve" and nothing happens.
2. **Pending resumes** (max-turns pause state, an SDK ``RunState``). A
   "continue" message routed elsewhere silently loses the seamless resume.
3. **The session LRU cache**. Message history is write-through to the DB, so
   every replica CAN load the truth — but a replica that cached the session
   before another replica appended messages keeps serving the stale copy.

## The mechanism

Following the platform's coordination doctrine (production architecture §4.5):
ownership stays local, only the FACTS become cross-replica visible.

* Existence of a pending approval / resume is a short-lived Redis key
  (``{prefix}:agent:pending:{kind}:{session_id}[:{request_id}]``, value = the
  owning replica id, TTL = the local registry's TTL).
* A resolution / resume / cache-write that belongs to another replica is
  broadcast on one control channel (``{prefix}:agent:session_ctl``); every
  replica hears it and the OWNER acts on it (broadcast + local filtering, same
  trade-off as DistributedRunRegistry's cancel — no routing table to go stale).
* Cache writes broadcast an invalidation; every OTHER replica drops its cached
  copy so its next read goes back to the DB.

Degradation: everything here is best-effort. Without Redis, approvals resolve
only on the owning replica, resumes stay local, and caches may go stale until
eviction — exactly the single-replica behaviour the service had before. Nothing
raises.
"""

from __future__ import annotations

import asyncio
import json
import time
import typing as t

from datacode_common.redis.client import get_redis, redis_key
from datacode_common.redis.pubsub import PatternPump, replica_id

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Control channel: one pub/sub channel for all cross-replica session events
# (invalidations, approval routing, resume forwarding).
_CHANNEL = redis_key("agent", "session_ctl")

# How often the invalidation broadcast may fire per session. A chat turn
# appends many messages; publishing one invalidation per message would flood
# the channel with redundant copies of the same fact.
_INVALIDATION_THROTTLE_SECONDS = 1.0

# Pending kinds → the Redis key suffix and the registry TTLs of their owners
# (approval.py / command_approval.py / resume.py keep the authoritative values;
# these mirror them so a key never outlives its local entry).
KIND_SDK_APPROVAL = "approval"
KIND_COMMAND_APPROVAL = "cmd"
KIND_RESUME = "resume"


def _pending_key(kind: str, session_id: str, request_id: str = "") -> str:
    if request_id:
        return redis_key("agent", "pending", kind, session_id, request_id)
    return redis_key("agent", "pending", kind, session_id)


def _spawn(coro) -> None:
    """Schedule a coroutine from a synchronous caller; drop it without a loop.

    Registration points (approval registries) are synchronous functions called
    from async contexts. If no loop is running there is nothing to broadcast
    to anyway — closing the coroutine avoids an un-awaited warning.
    """
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


class SessionCoordination:
    """Owns the session-state control channel and the pending-existence keys."""

    def __init__(self, client=None) -> None:
        # Optional injected Redis client (tests). Production resolves the
        # shared per-loop client on every call.
        self._client = client
        self._pump: t.Optional[PatternPump] = None
        self._last_invalidation: dict[str, float] = {}
        self._last_degrade_warn = 0.0

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the control channel (no-op when Redis is disabled)."""
        if self._pump is not None:
            return
        if self._client is None and get_redis() is None:
            logger.info("session_coordination_disabled", reason="platform_redis_disabled")
            return
        self._pump = PatternPump(
            domain="agent-session-ctl",
            pattern=_CHANNEL,
            on_message=self._on_message,
            client=self._client,
        )
        await self._pump.start()
        logger.info("session_coordination_started")

    async def stop(self) -> None:
        if self._pump is not None:
            await self._pump.stop()
            self._pump = None
            logger.info("session_coordination_stopped")

    # --- pending-existence registry -------------------------------------------

    async def register_pending(
        self, kind: str, session_id: str, request_id: str, ttl_seconds: int
    ) -> None:
        """Make a pending approval/resume visible to every replica."""
        try:
            client = self._redis()
            if client is None:
                return
            await client.set(
                _pending_key(kind, session_id, request_id), replica_id(), ex=ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001 - coordination is best-effort
            self._warn_degraded("register_pending", exc)

    async def unregister_pending(
        self, kind: str, session_id: str, request_id: str
    ) -> None:
        """Drop the existence key (resolved / consumed / cancelled)."""
        try:
            client = self._redis()
            if client is None:
                return
            await client.delete(_pending_key(kind, session_id, request_id))
        except Exception as exc:  # noqa: BLE001
            self._warn_degraded("unregister_pending", exc)

    async def pending_exists(
        self, kind: str, session_id: str, request_id: str = ""
    ) -> bool:
        """Whether a pending entry exists on ANY replica (including this one)."""
        try:
            client = self._redis()
            if client is None:
                return False
            return bool(await client.exists(_pending_key(kind, session_id, request_id)))
        except Exception as exc:  # noqa: BLE001
            self._warn_degraded("pending_exists", exc)
            return False

    async def resume_pending_remotely(self, session_id: str) -> bool:
        """Whether another replica holds a paused run for this session.

        Callers check their OWN registry first; a local pending resume is
        always preferable (no forwarding hop).
        """
        return await self.pending_exists(KIND_RESUME, session_id)

    # --- broadcasts -------------------------------------------------------------

    def broadcast_cache_invalidation(self, session_id: str) -> None:
        """Tell other replicas their cached copy of a session is stale.

        Throttled per session: within the window a later invalidation carries
        the same fact (the cache is dropped, not versioned), so redundant
        publishes only add channel noise.
        """
        now = time.monotonic()
        if now - self._last_invalidation.get(session_id, 0.0) < _INVALIDATION_THROTTLE_SECONDS:
            return
        self._last_invalidation[session_id] = now
        _spawn(self._broadcast("invalidate", session_id=session_id))

    def broadcast_resume_chat(self, session_id: str, request_payload: dict) -> None:
        """Forward a resume-intent chat request to the replica holding the run state."""
        _spawn(self._broadcast("resume_chat", session_id=session_id, request=request_payload))

    async def _broadcast(self, action: str, **fields: t.Any) -> None:
        try:
            client = self._redis()
            if client is None:
                return
            await client.publish(
                _CHANNEL, json.dumps({"origin": replica_id(), "action": action, **fields})
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_degraded("broadcast", exc)

    # --- approval resolution (local + routed) -----------------------------------

    async def resolve_approval(
        self, session_id: str, request_id: str, approved: bool, reason: str = ""
    ) -> t.Optional[dict]:
        """Resolve an approval wherever its future lives.

        Returns a response dict for the endpoint:
        - ``{"status": "resolved", ...}``  — resolved on THIS replica;
        - ``{"status": "forwarded"}``      — a remote replica owns it and was
          asked to resolve (fire-and-forget, like run cancellation);
        - ``None``                         — no replica knows this request (404).
        """
        resolved = await self._resolve_locally(session_id, request_id, approved, reason)
        if resolved is not None:
            return resolved

        # Not here: is it pending anywhere else? The existence keys answer
        # without hoping a broadcast finds an owner.
        for kind in (KIND_COMMAND_APPROVAL, KIND_SDK_APPROVAL):
            if await self.pending_exists(kind, session_id, request_id):
                _spawn(
                    self._broadcast(
                        "resolve_approval",
                        session_id=session_id,
                        request_id=request_id,
                        approved=approved,
                        reason=reason,
                    )
                )
                logger.info(
                    "approval_resolution_forwarded",
                    session_id=session_id,
                    request_id=request_id,
                    kind=kind,
                )
                return {"status": "forwarded", "request_id": request_id, "approved": approved}
        return None

    async def _resolve_locally(
        self, session_id: str, request_id: str, approved: bool, reason: str
    ) -> t.Optional[dict]:
        """Try both local approval registries; publish + resume on success."""
        from src.context.session import session_manager
        from src.protocol.messages import human_response
        from src.runtime.approval import get_pending_approval, resolve_pending_approval
        from src.runtime.timeline import publish_and_persist
        from src.tools.command_approval import resolve_command_approval

        if resolve_command_approval(session_id, request_id, approved):
            await publish_and_persist(
                session_id,
                human_response(
                    session_id, request_id=request_id, approved=approved, reason=reason or ""
                ),
            )
            return {"status": "resolved", "request_id": request_id, "approved": approved,
                    "type": "command_approval"}

        pending = get_pending_approval(session_id, request_id)
        if not resolve_pending_approval(session_id, request_id, approved=approved):
            return None

        await publish_and_persist(
            session_id,
            human_response(
                session_id, request_id=request_id, approved=approved, reason=reason or ""
            ),
        )
        if pending is not None:
            session = await session_manager.get_session_async(session_id)
            if session is not None:
                from src.runtime.active_runs import claim_chat_run, track_chat_run
                from src.runtime.streaming import resume_after_approval
                from src.utils.tasks import spawn_background_task

                # The approved tool executes in a new run: claim the session so
                # a concurrent POST /chat is refused, and track the task so the
                # claim is released when it ends.
                await claim_chat_run(session_id)
                task = spawn_background_task(
                    resume_after_approval(session, pending, approved),
                    name=f"resume_after_approval:{session_id}",
                )
                track_chat_run(session_id, task, agent_name=session.mode.value)
        return {"status": "resolved", "request_id": request_id, "approved": approved}

    # --- forwarded chat execution -------------------------------------------------

    async def launch_chat_run(self, session, req) -> asyncio.Task:
        """Spawn a chat run and track it exactly like the POST /chat endpoint.

        Shared by the endpoint and by forwarded resume requests so both paths
        get the same run-exclusivity registration and cleanup.
        """
        from src.runtime.active_runs import claim_chat_run, track_chat_run
        from src.runtime.chat_executor import execute_chat

        session_id = session.session_id
        # A forwarded resume runs whatever the registry says (the paused run it
        # continues has already released its key), but it still claims the
        # session for its own duration.
        await claim_chat_run(session_id)
        task = asyncio.create_task(execute_chat(session, req))
        track_chat_run(session_id, task, agent_name=session.mode.value)
        return task

    async def _launch_forwarded_chat(self, session_id: str, request_payload: dict) -> None:
        """Owner side of a forwarded resume: run it here, events fan out."""
        from src.api.schemas import ChatMessageRequest
        from src.context.session import session_manager
        from src.protocol.messages import system_warning
        from src.runtime.active_runs import register_chat_run
        from src.runtime.timeline import publish_and_persist

        try:
            req = ChatMessageRequest(**request_payload)
        except Exception as exc:  # noqa: BLE001 - malformed forward is dropped
            logger.warning("forwarded_chat_request_invalid", session_id=session_id, error=str(exc))
            return

        session = await session_manager.get_session_async(session_id)
        if session is None:
            logger.warning("forwarded_chat_session_missing", session_id=session_id)
            return

        if not await register_chat_run(session_id):
            await publish_and_persist(
                session_id,
                system_warning(
                    session_id,
                    code="CHAT_RUN_BUSY",
                    message="A chat run is already in progress for this session",
                ),
            )
            return

        await self.launch_chat_run(session, req)
        logger.info("forwarded_resume_chat_started", session_id=session_id)

    # --- message pump ------------------------------------------------------------

    async def _on_message(self, channel: str, payload: str) -> None:
        """Dispatch one control message; own messages are already delivered locally."""
        try:
            message = json.loads(payload)
        except ValueError:
            logger.warning("session_ctl_malformed_message", channel=channel)
            return
        if message.get("origin") == replica_id():
            return

        action = message.get("action")
        session_id = message.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return

        if action == "invalidate":
            from src.context.session import session_manager

            session_manager.invalidate_cached_session(session_id)
        elif action == "resolve_approval":
            await self._resolve_locally(
                session_id,
                str(message.get("request_id") or ""),
                bool(message.get("approved")),
                str(message.get("reason") or ""),
            )
        elif action == "resume_chat":
            request = message.get("request")
            if isinstance(request, dict):
                await self._launch_forwarded_chat(session_id, request)
        # Unknown actions are ignored: a newer producer talking to an older
        # consumer must not break the channel.

    # --- internals -----------------------------------------------------------------

    def _redis(self):
        if self._client is not None:
            return self._client
        return get_redis()

    def _warn_degraded(self, op: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_degrade_warn < 60.0:
            return
        self._last_degrade_warn = now
        logger.warning(
            "session_coordination_degraded",
            op=op,
            error=str(exc),
            detail="Redis unavailable: approvals/resumes resolve on their owning "
            "replica only, and session caches may serve stale copies until eviction.",
        )


# Global singleton (import lazily inside call sites that live in import cycles).
_coordination: t.Optional[SessionCoordination] = None


def get_session_coordination() -> SessionCoordination:
    global _coordination
    if _coordination is None:
        _coordination = SessionCoordination()
    return _coordination
