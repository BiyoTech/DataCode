"""Session Manager — manages ChatSession lifecycle with async SQLite persistence.

Each ChatSession maintains:
- Session metadata (user, workspace, mode, status)
- Message history (persisted to SQLite) with sliding-window compression
- Workspace context (rules, memory from .agent/ directory, cached)
- Current agent state

Uses an in-memory cache layer for active sessions with async DB write-through.
"""

import asyncio
import json
import os
import typing as t
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.config import settings
from src.context.execution_style import ExecutionStyle
from src.tools import workspace_fs
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Maximum number of recent messages to include in context before summarization kicks in
MAX_RECENT_MESSAGES = 30


def _reap_pending_registries() -> None:
    """Lazily clean up expired pending approvals / resumes.

    Imported here to avoid a module-level circular import (approval/resume
    import from context.session indirectly). Safe to call from any context;
    both helpers are pure dict mutations with no I/O.
    """
    try:
        from src.runtime.approval import cleanup_expired_approvals
        from src.runtime.resume import cleanup_expired_resumes

        a = cleanup_expired_approvals()
        r = cleanup_expired_resumes()
        if a or r:
            logger.info(
                "pending_registries_reaped",
                approvals_removed=a,
                resumes_removed=r,
            )
    except Exception as e:  # noqa: BLE001 - housekeeping must never break session load
        logger.warning("pending_registries_cleanup_failed", error=str(e))

# Maximum characters persisted for a single tool output (keeps DB rows / context bounded)
MAX_PERSISTED_TOOL_OUTPUT_CHARS = 2000


def _coerce_item_to_dict(item: t.Any) -> t.Optional[t.Dict[str, t.Any]]:
    """Best-effort conversion of an SDK input item into a plain dict.

    SDK ``to_input_list()`` items may be pydantic models or plain dicts. We only
    need a dict view to read fields like ``type`` / ``call_id`` / ``content``.
    Falls back through several serialization strategies so a tool output item is
    never dropped (a dropped output orphans its assistant.tool_calls and later
    triggers a 400 from the LLM API).
    """
    if isinstance(item, dict):
        return item
    # Pydantic v2 models — exclude unset so optional fields keep their names.
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    # Pydantic v1 models.
    dump_v1 = getattr(item, "dict", None)
    if callable(dump_v1):
        try:
            return dump_v1()
        except Exception:
            pass
    # Last resort: build a dict from public attributes so we can still read
    # `type` / `call_id` / `output` rather than discarding the item entirely.
    if hasattr(item, "__dict__"):
        try:
            return {k: v for k, v in vars(item).items() if not k.startswith("_")}
        except Exception:
            return None
    return None


def _safe_window_start(
    messages: t.List[t.Dict[str, t.Any]], max_recent: int
) -> int:
    """Compute a sliding-window start index that never splits a tool group.

    A naive ``len - max_recent`` boundary can land between an
    ``assistant.tool_calls`` message and the ``tool`` outputs responding to it,
    stranding a tool_call without its output and triggering a 400 from the LLM
    API. We therefore snap the start backwards until it does not sit on a
    ``tool`` message and is not immediately preceded by an unmatched
    ``assistant.tool_calls`` group. The window only grows (never shrinks), so a
    bit more context is kept rather than risking a malformed sequence.
    """
    start = max(0, len(messages) - max_recent)
    # Walk backwards while the boundary would cut inside a tool group: either the
    # first kept message is a `tool` output (its declaring assistant is older),
    # or the message just before the boundary is an assistant that issued
    # tool_calls (its outputs would be inside the window without the declaration
    # when start lands on the outputs). Snapping to the assistant.tool_calls keeps
    # the whole group together.
    while start > 0:
        first = messages[start]
        if first.get("role") == "tool":
            start -= 1
            continue
        prev = messages[start - 1]
        if prev.get("tool_calls"):
            start -= 1
            continue
        break
    return start


def _extract_assistant_text(content: t.Any) -> str:
    """Extract plain text from a Responses-API assistant message ``content``.

    ``content`` may be a string or a list of content blocks
    (e.g. ``{"type": "output_text", "text": "..."}``).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _sanitize_tool_message_sequence(
    messages: t.List[t.Dict[str, t.Any]],
) -> t.List[t.Dict[str, t.Any]]:
    """Guarantee a well-formed assistant/tool message sequence before sending to the LLM.

    The ChatCompletions API rejects any ``tool`` message that is not a response to a
    preceding ``assistant`` message carrying ``tool_calls`` (and vice versa, a
    ``tool_calls`` entry must be followed by its output). Interrupted runs (e.g.
    max_turns / approval) or out-of-order persistence can leave the stored history
    in a broken state. This pass repairs it deterministically:

    - Drop orphan ``tool`` outputs whose ``tool_call_id`` was never declared by an
      earlier ``assistant.tool_calls`` (the case behind the observed 400 error).
    - Drop ``tool_calls`` entries that have no matching ``tool`` output later in the
      sequence; if an assistant message is left with neither calls nor text, drop it.

    The two rules are applied consistently so every kept ``tool`` has a kept
    declaring ``assistant.tool_calls`` and vice versa.
    """
    # Pass 1 (forward): determine which tool outputs are valid — their call_id must
    # have been declared by an assistant.tool_calls appearing earlier.
    declared_ids: t.Set[str] = set()
    valid_output_ids: t.Set[str] = set()
    for msg in messages:
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id:
                    declared_ids.add(tc_id)
        elif msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            if call_id and call_id in declared_ids:
                valid_output_ids.add(call_id)

    # Pass 2 (build): keep only consistent calls/outputs.
    sanitized: t.List[t.Dict[str, t.Any]] = []
    dropped_calls = 0
    dropped_outputs = 0
    for msg in messages:
        if msg.get("tool_calls"):
            kept_calls = [tc for tc in msg["tool_calls"] if tc.get("id") in valid_output_ids]
            dropped_calls += len(msg["tool_calls"]) - len(kept_calls)
            if kept_calls:
                new_msg = dict(msg)
                new_msg["tool_calls"] = kept_calls
                sanitized.append(new_msg)
            elif msg.get("content"):
                # Preserve any assistant text, drop the dangling tool_calls.
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                sanitized.append(new_msg)
            # else: fully dangling assistant turn -> drop
        elif msg.get("role") == "tool":
            if msg.get("tool_call_id") in valid_output_ids:
                sanitized.append(msg)
            else:
                dropped_outputs += 1
        else:
            sanitized.append(msg)

    if dropped_calls or dropped_outputs:
        logger.warning(
            "message_sequence_sanitized",
            dropped_tool_calls=dropped_calls,
            dropped_tool_outputs=dropped_outputs,
        )
    return sanitized


class InteractionMode(str, Enum):
    """Interaction mode — determines agent behavior, tool access, and UI rendering."""
    ASK = "ask"
    CRAFT = "craft"
    DESIGN = "design"


@dataclass
class WorkspaceContext:
    """Workspace context info — injected into Agent and Tool.

    Loaded from .agent/ directory configuration, rules, and memory.
    """
    workspace_path: str
    agent_dir: str = ""
    rules: t.List[str] = field(default_factory=list)
    memory_index: str = ""
    config: t.Dict[str, t.Any] = field(default_factory=dict)

    def __post_init__(self):
        self.agent_dir = os.path.join(self.workspace_path, ".agent")


@dataclass
class SessionContext:
    """Agent session context — domain model for a ChatSession.

    workspace_path and environment are NOT persisted in the DB. They are
    runtime parameters set per chat message and injected into agent execution.

    scenario is persisted and determines the entry agent ('dev' -> triage,
    'chatbi' -> data_analysis_agent) and session-list filtering.
    """
    session_id: str
    user_id: str
    title: str = ""
    mode: InteractionMode = InteractionMode.ASK
    execution_style: ExecutionStyle = ExecutionStyle.AGENT  # autonomy dimension
    status: str = "active"
    scenario: str = "dev"  # 'dev' | 'chatbi'
    message_count: int = 0
    messages: t.List[t.Dict[str, t.Any]] = field(default_factory=list)
    # Track last summarized message_count for dedup in _memory_post_process
    last_summarized_message_count: t.Optional[int] = None
    # Runtime-only fields (not persisted in sessions table)
    workspace_path: str = ""
    workspace_context: t.Optional[WorkspaceContext] = None
    current_agent: str = "triage"
    environment: str = "prod"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: t.Optional[datetime] = None


class SessionManager:
    """Manages ChatSession instances with async SQLite persistence.

    Responsibilities:
    - Create / get / list sessions (with DB write-through)
    - Manage message history (persisted per message)
    - Initialize .agent/ directory
    - Load workspace rules and memory
    """

    def __init__(self):
        # In-memory LRU cache for active sessions (fast path). OrderedDict keeps
        # insertion/access order so the oldest entries can be evicted first.
        self._sessions: "OrderedDict[str, SessionContext]" = OrderedDict()
        # Workspace context LRU cache to avoid repeated file I/O.
        self._workspace_context_cache: "OrderedDict[str, WorkspaceContext]" = OrderedDict()
        # Per-session locks serializing DB load-and-cache so two concurrent
        # requests for the same uncached session don't both hit the DB and
        # clobber each other's cached object. Keyed by session_id.
        self._load_locks: t.Dict[str, asyncio.Lock] = {}

    # ================================================================
    # Cache management (LRU eviction)
    # ================================================================

    def _cache_session(self, session: SessionContext) -> None:
        """Insert/refresh a session in the LRU cache and evict if over capacity."""
        self._sessions[session.session_id] = session
        self._sessions.move_to_end(session.session_id)
        self._evict_sessions_if_needed()

    def _touch_session(self, session_id: str) -> t.Optional[SessionContext]:
        """Return a cached session (if present) and mark it most-recently-used."""
        session = self._sessions.get(session_id)
        if session is not None:
            self._sessions.move_to_end(session_id)
        return session

    def _evict_sessions_if_needed(self) -> None:
        """Evict oldest *inactive* sessions until within the configured cap.

        Active sessions (an in-flight chat run) are skipped so their in-memory
        state is never dropped mid-run; evicting them could otherwise let a
        concurrent reload create a divergent copy. Evicted sessions are reloaded
        from the DB on next access.
        """
        max_size = settings.session_cache_max_size
        if max_size <= 0 or len(self._sessions) <= max_size:
            return

        # Import here to avoid a module-level import cycle at startup.
        from src.runtime.active_runs import active_chat_runs

        # Iterate oldest-first; skip active runs. Stop when either within cap or
        # every remaining session is active (nothing more is evictable).
        for session_id in list(self._sessions.keys()):
            if len(self._sessions) <= max_size:
                break
            if session_id in active_chat_runs:
                continue
            self._sessions.pop(session_id, None)
            logger.info("session_evicted_from_cache", session_id=session_id)

    def _cache_workspace_context(self, ctx: WorkspaceContext) -> None:
        """Insert/refresh a workspace context in the LRU cache and evict if over capacity."""
        key = ctx.workspace_path
        self._workspace_context_cache[key] = ctx
        self._workspace_context_cache.move_to_end(key)
        max_size = settings.workspace_context_cache_max_size
        if max_size > 0:
            while len(self._workspace_context_cache) > max_size:
                evicted_path, _ = self._workspace_context_cache.popitem(last=False)
                logger.info("workspace_context_evicted_from_cache", workspace_path=evicted_path)

    def invalidate_cached_session(self, session_id: str) -> bool:
        """Drop a session from the local cache (cross-replica invalidation).

        Called when ANOTHER replica wrote to the session: our cached copy is
        now stale, and the next read must go back to the DB. Skipped while a
        chat run is active on THIS replica — the running task holds the object
        and its final writes re-cache the authoritative state, so dropping it
        mid-run would only churn.
        """
        from src.runtime.active_runs import active_chat_runs

        if session_id in active_chat_runs:
            return False
        evicted = self._sessions.pop(session_id, None)
        if evicted is not None:
            logger.info("session_cache_invalidated", session_id=session_id)
        return evicted is not None

    @staticmethod
    def _broadcast_invalidation(session_id: str) -> None:
        """Tell other replicas their cached copy went stale (best-effort).

        Throttled inside the coordination layer, so calling this per message is
        cheap even though a chat turn appends many.
        """
        from src.runtime.session_coordination import get_session_coordination

        get_session_coordination().broadcast_cache_invalidation(session_id)

    # ================================================================
    # Public API
    # ================================================================

    def create_session(
        self,
        user_id: str,
        title: str = "",
        mode: str = "ask",
        scenario: str = "dev",
        execution_style: str = "",
    ) -> SessionContext:
        """Create a new ChatSession (in-memory only).

        workspace_path and environment are set later per chat message.
        scenario ('dev' | 'chatbi') drives entry-agent selection.
        execution_style ('agent' | 'interactive') drives autonomy; defaults to agent.
        """
        session_id = str(uuid.uuid4())

        session = SessionContext(
            session_id=session_id,
            user_id=user_id,
            title=title or f"Chat {session_id[:8]}",
            mode=InteractionMode(mode),
            execution_style=ExecutionStyle.from_value(execution_style),
            scenario=scenario,
        )
        self._cache_session(session)

        logger.info(
            "session_created",
            session_id=session_id,
            user_id=user_id,
            mode=mode,
            execution_style=session.execution_style.value,
            scenario=scenario,
        )
        return session

    async def create_session_async(
        self,
        user_id: str,
        title: str = "",
        mode: str = "ask",
        scenario: str = "dev",
        execution_style: str = "",
    ) -> SessionContext:
        """Create a new ChatSession with database persistence."""
        session = self.create_session(
            user_id, title, mode, scenario=scenario, execution_style=execution_style
        )
        await self._persist_session(session)
        return session

    async def delete_session_async(self, session_id: str) -> bool:
        """Delete a session and all associated data.

        Cascade-deletes: messages, timeline_events (via FK), memory_events,
        and memory_summaries (manual cascade since they lack FK on session_id).
        Returns True if deleted.
        """
        from sqlalchemy import delete, select

        from src.context.database import (
            MemoryEventRow,
            MemorySummaryRow,
            SessionRow,
            get_session_factory,
        )

        # Remove from in-memory cache
        self._sessions.pop(session_id, None)

        try:
            async with get_session_factory()() as db:
                # Manually cascade-delete memory summaries whose event belongs to this session
                await db.execute(
                    delete(MemorySummaryRow).where(
                        MemorySummaryRow.event_id.in_(
                            select(MemoryEventRow.id).where(
                                MemoryEventRow.session_id == session_id
                            )
                        )
                    )
                )
                # Delete memory events for this session
                await db.execute(
                    delete(MemoryEventRow).where(MemoryEventRow.session_id == session_id)
                )

                # SessionRow cascades messages + timeline_events via FK ON DELETE CASCADE
                stmt = delete(SessionRow).where(SessionRow.id == session_id)
                result = await db.execute(stmt)
                await db.commit()
                deleted = result.rowcount > 0
                if deleted:
                    self._broadcast_invalidation(session_id)
                    logger.info("session_deleted", session_id=session_id)
                else:
                    logger.warning("session_delete_not_found", session_id=session_id)
                return deleted
        except Exception as e:
            logger.error("session_delete_failed", session_id=session_id, error=str(e))
            return False

    async def update_session_title_async(self, session_id: str, title: str) -> bool:
        """Update session title (used for rename or auto-title generation)."""
        from sqlalchemy import update

        from src.context.database import SessionRow, get_session_factory

        # Update in-memory cache
        session = self._sessions.get(session_id)
        if session:
            session.title = title

        try:
            async with get_session_factory()() as db:
                stmt = (
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(title=title)
                )
                await db.execute(stmt)
                await db.commit()
                self._broadcast_invalidation(session_id)
                logger.info("session_title_updated", session_id=session_id, title=title)
                return True
        except Exception as e:
            logger.error("session_title_update_failed", session_id=session_id, error=str(e))
            return False

    def get_session(self, session_id: str) -> t.Optional[SessionContext]:
        """Get session from in-memory cache."""
        return self._touch_session(session_id)

    async def get_session_async(self, session_id: str) -> t.Optional[SessionContext]:
        """Get session — check cache first, then load from DB if missing.

        A per-session lock serializes the load-and-cache step so concurrent
        requests for the same uncached session don't both query the DB and
        overwrite each other's cached object with divergent copies.
        """
        cached = self._touch_session(session_id)
        if cached:
            # Lazily reap expired pending approvals / resumes so the registries
            # don't accumulate entries from abandoned sessions.
            _reap_pending_registries()
            return cached

        lock = self._load_locks.setdefault(session_id, asyncio.Lock())
        try:
            async with lock:
                # Re-check: another coroutine may have loaded it while we waited.
                cached = self._touch_session(session_id)
                if cached:
                    _reap_pending_registries()
                    return cached

                session = await self._load_session_from_db(session_id)
                if session:
                    self._cache_session(session)
                _reap_pending_registries()
                return session
        finally:
            # Clean up the lock on the common uncontended path so _load_locks
            # doesn't accumulate one entry per session ever loaded. Any waiter
            # already holds its own reference to this lock object, so removing
            # it from the dict is safe; at worst a brand-new caller briefly
            # creates a second lock, which the cache double-check absorbs.
            if not lock.locked() and session_id in self._load_locks:
                self._load_locks.pop(session_id, None)

    def set_mode(self, session_id: str, mode: str) -> bool:
        """Switch session interaction mode."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.mode = InteractionMode(mode)
        logger.info("session_mode_changed", session_id=session_id, mode=mode)
        return True

    def set_execution_style(self, session_id: str, execution_style: str) -> bool:
        """Switch session execution style (agent / interactive) — in-memory only."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.execution_style = ExecutionStyle.from_value(execution_style)
        logger.info(
            "session_execution_style_changed",
            session_id=session_id,
            execution_style=session.execution_style.value,
        )
        return True

    async def set_execution_style_async(self, session_id: str, execution_style: str) -> bool:
        """Switch session execution style and persist it to the database."""
        from sqlalchemy import update

        from src.context.database import SessionRow, get_session_factory

        session = await self.get_session_async(session_id)
        if not session:
            return False

        style = ExecutionStyle.from_value(execution_style)
        session.execution_style = style

        try:
            async with get_session_factory()() as db:
                stmt = (
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(execution_style=style.value)
                )
                await db.execute(stmt)
                await db.commit()
            self._broadcast_invalidation(session_id)
            logger.info(
                "session_execution_style_persisted",
                session_id=session_id,
                execution_style=style.value,
            )
            return True
        except Exception as e:
            logger.error(
                "session_execution_style_persist_failed",
                session_id=session_id,
                error=str(e),
            )
            return False

    async def persist_runtime_context_async(self, session: SessionContext) -> None:
        """Persist last-known workspace/environment for session reload."""
        from sqlalchemy import update

        from src.context.database import SessionRow, get_session_factory

        try:
            async with get_session_factory()() as db:
                stmt = (
                    update(SessionRow)
                    .where(SessionRow.id == session.session_id)
                    .values(
                        last_workspace_path=session.workspace_path or "",
                        last_environment=session.environment or "prod",
                    )
                )
                await db.execute(stmt)
                await db.commit()
            logger.info(
                "session_runtime_context_persisted",
                session_id=session.session_id,
                workspace_path=session.workspace_path,
                environment=session.environment,
            )
        except Exception as e:
            logger.error(
                "session_runtime_context_persist_failed",
                session_id=session.session_id,
                error=str(e),
            )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: t.Optional[t.Dict] = None,
        tool_calls: t.Optional[t.List[t.Dict]] = None,
        tool_call_id: str = "",
    ) -> None:
        """Append message to session history (in-memory).

        Supports OpenAI-format tool interactions:
        - assistant messages with tool_calls
        - tool messages with tool_call_id
        """
        session = self._sessions.get(session_id)
        if not session:
            logger.warning("session_not_found", session_id=session_id)
            return

        msg: t.Dict[str, t.Any] = {
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id

        session.messages.append(msg)
        session.message_count += 1
        session.last_message_at = datetime.now(timezone.utc)

    async def add_message_async(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: t.Optional[t.Dict] = None,
        tool_calls: t.Optional[t.List[t.Dict]] = None,
        tool_call_id: str = "",
    ) -> None:
        """Append message to session history and persist to DB."""
        self.add_message(
    session_id, role, content, metadata,
    tool_calls=tool_calls, tool_call_id=tool_call_id,
)
        await self._persist_message(
    session_id, role, content, metadata,
    tool_calls=tool_calls, tool_call_id=tool_call_id,
)
        # Other replicas may hold this session in cache: their copy is stale now.
        self._broadcast_invalidation(session_id)

    async def persist_run_items_async(
        self,
        session_id: str,
        items: t.List[t.Any],
    ) -> None:
        """Persist a run's new items (SDK Responses-API format) as stored messages.

        This is the single source of truth for tool-turn persistence. Items come from
        ``streamed_result.to_input_list()`` (delta of a finished/interrupted run) and are
        guaranteed well-ordered by the SDK. The conversion:

        - Groups consecutive ``function_call`` items into ONE ``assistant`` message with a
          ``tool_calls`` array, so parallel tool calls keep a single declaring assistant
          turn (this is what prevents the orphaned-``tool`` 400 error).
        - Emits each ``function_call_output`` as a ``tool`` message after flushing the
          buffered assistant turn.
        - Emits assistant text ``message`` items as plain assistant messages.
        - Skips reasoning / unsupported items (not needed for ChatCompletions replay).
        """
        pending_calls: t.List[t.Dict[str, t.Any]] = []

        async def _flush_pending_calls() -> None:
            if pending_calls:
                await self.add_message_async(
                    session_id, "assistant", "", tool_calls=list(pending_calls),
                )
                pending_calls.clear()

        for raw in items:
            item = _coerce_item_to_dict(raw)
            if item is None:
                continue
            item_type = item.get("type")

            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id") or ""
                if not call_id:
                    continue
                pending_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        # arguments is already a JSON string in Responses format
                        "arguments": item.get("arguments", "") or "{}",
                    },
                })

            elif item_type == "function_call_output":
                await _flush_pending_calls()
                # Match the fallback chain used for function_call so an output is
                # never silently dropped when the SDK names the field `id`. A
                # dropped output would leave its declaring assistant.tool_calls
                # without a response and later trigger a 400 from the LLM API.
                call_id = item.get("call_id") or item.get("id") or ""
                if not call_id:
                    logger.warning(
                        "tool_output_missing_call_id_dropped",
                        session_id=session_id,
                        item_keys=list(item.keys()),
                    )
                    continue
                output = item.get("output", "")
                # Persist an empty output as an empty string rather than skipping
                # the message: the tool_call_id pairing must always be satisfied.
                output_str = str(output)[:MAX_PERSISTED_TOOL_OUTPUT_CHARS] if output else ""
                await self.add_message_async(
                    session_id, "tool", output_str, tool_call_id=call_id,
                )

            elif item_type == "message" or item.get("role") == "assistant":
                await _flush_pending_calls()
                text = _extract_assistant_text(item.get("content"))
                if text:
                    await self.add_message_async(session_id, "assistant", text)

            # Other item types (reasoning, etc.) are intentionally skipped.

        # Flush any trailing tool_calls (e.g. a turn interrupted before its outputs).
        await _flush_pending_calls()

    def get_messages(self, session_id: str, workspace_path: str = "") -> t.List[t.Dict[str, t.Any]]:
        """Get conversation history with sliding-window compression.

        Keeps the last MAX_RECENT_MESSAGES; older messages are summarized
        and injected as a context prefix. Tool calls are included in full format.
        """
        session = self._sessions.get(session_id)
        if not session:
            return []

        all_messages = session.messages
        if len(all_messages) <= MAX_RECENT_MESSAGES:
            # Sanitize before formatting so the LLM never receives a broken
            # tool/tool_calls sequence (defense layer against interrupted runs).
            return self._format_messages_for_sdk(_sanitize_tool_message_sequence(all_messages))

        # Split: recent messages kept as-is, older ones summarized. Snap the
        # boundary backwards so it never falls between an assistant.tool_calls
        # message and the tool outputs responding to it — cutting inside a tool
        # group would strand a tool_call without its output (or vice versa) and
        # produce the 400 "tool_call_ids did not have response messages" error.
        split = _safe_window_start(all_messages, MAX_RECENT_MESSAGES)
        older = all_messages[:split]
        recent = all_messages[split:]

        # Build summary from older messages (async wrapper handled in main.py)
        result: t.List[t.Dict[str, t.Any]] = []

        # Inject summary as context if older messages were summarized (L2 events exist)
        if session.last_summarized_message_count is not None:
            summary_text = (
    f"[Earlier conversation covered {len(older)} messages. Key events are "
    "available in memory — use memory_read for details.]"
)
            result.append({
                "role": "user",
                "content": f"[Previous conversation context]\n{summary_text}",
            })

        # Sanitize the recent window — a sliding window can cut between an
        # assistant.tool_calls and its outputs, producing orphans.
        result.extend(self._format_messages_for_sdk(_sanitize_tool_message_sequence(recent)))
        return result

    def _format_messages_for_sdk(
    self, messages: t.List[t.Dict[str, t.Any]]
) -> t.List[t.Dict[str, t.Any]]:
        """Format messages into openai-agents SDK (Responses API) input items.

        The SDK treats `input` as a list of Responses API items, NOT ChatCompletions
        messages. Its converter only accepts plain messages with exactly {role, content}
        keys; tool interactions must be expressed as dedicated items:
        - tool call   -> {"type": "function_call", "call_id", "name", "arguments"}
        - tool output -> {"type": "function_call_output", "call_id", "output"}

        We persist tool turns in ChatCompletions shape (assistant.tool_calls / role=tool),
        so they are translated here. Failing to do so raises
        "Unhandled item type or structure" inside the SDK converter.
        """
        formatted: t.List[t.Dict[str, t.Any]] = []
        for msg in messages:
            role = msg.get("role", "")

            # 1) Tool output message -> Responses function_call_output item
            if role == "tool" and msg.get("tool_call_id"):
                formatted.append({
                    "type": "function_call_output",
                    "call_id": msg["tool_call_id"],
                    "output": msg.get("content", ""),
                })
                continue

            # 2) Assistant message carrying tool calls -> one function_call item each
            if msg.get("tool_calls"):
                # Preserve any assistant text as a separate plain message
# (keep {role, content} only)
                content = msg.get("content")
                if content:
                    formatted.append({"role": "assistant", "content": content})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    formatted.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        # arguments is already a JSON string at persistence time
                        "arguments": fn.get("arguments", "") or "{}",
                    })
                continue

            # 3) Plain message — emit exactly {role, content} so the converter accepts it
            formatted.append({"role": role, "content": msg.get("content", "")})
        return formatted

    def list_sessions(
        self,
        user_id: str,
    ) -> t.List[SessionContext]:
        """List user sessions from in-memory cache (no workspace filter)."""
        sessions = [
            s for s in self._sessions.values()
            if s.user_id == user_id
        ]
        return sorted(sessions, key=lambda s: s.created_at, reverse=True)

    async def list_sessions_async(
        self,
        user_id: str,
        scenario: t.Optional[str] = None,
    ) -> t.List[SessionContext]:
        """List all sessions for a user from DB (no workspace filter).

        If scenario is provided, filter sessions by scenario ('dev' | 'chatbi').
        """
        from src.context.database import SessionRow, get_session_factory

        async with get_session_factory()() as db:
            from sqlalchemy import select
            stmt = (
                select(SessionRow)
                .where(SessionRow.user_id == user_id)
                .order_by(SessionRow.created_at.desc())
            )
            if scenario:
                stmt = stmt.where(SessionRow.scenario == scenario)
            result = await db.execute(stmt)
            rows = result.scalars().all()

        sessions = []
        for row in rows:
            if row.id in self._sessions:
                sessions.append(self._sessions[row.id])
            else:
                # Build a lightweight SessionContext (without full message
                # history) for list display and cache it so subsequent GETs
                # don't re-query the DB. The full message history is loaded
                # lazily by _load_session_from_db on first real use.
                session = SessionContext(
                    session_id=row.id,
                    user_id=row.user_id,
                    title=row.title,
                    mode=InteractionMode(row.mode),
                    execution_style=ExecutionStyle.from_value(
                        getattr(row, "execution_style", None)
                    ),
                    status=row.status,
                    scenario=row.scenario or "dev",
                    message_count=row.message_count,
                    created_at=row.created_at,
                    last_message_at=row.updated_at,
                )
                # Cache the metadata-only session. _cache_session handles LRU
                # eviction; if a fuller version is later loaded by
                # get_session_async, it will overwrite this entry.
                self._cache_session(session)
                sessions.append(session)
        return sessions

    # ================================================================
    # Timeline Event Persistence
    # ================================================================

    async def add_timeline_event_async(
        self,
        session_id: str,
        event_type: str,
        event_id: str,
        parent_event_id: str = "",
        payload: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> None:
        """Persist an ACP timeline event to DB for frontend restoration.

        Called alongside event_bus.publish() during agent streaming. Events
        are loaded from DB when the frontend reconnects to rebuild the Timeline.
        """
        from src.context.database import TimelineEventRow, get_session_factory

        try:
            async with get_session_factory()() as db:
                row = TimelineEventRow(
                    session_id=session_id,
                    event_type=event_type,
                    event_id=event_id,
                    parent_event_id=parent_event_id or None,
                    payload_json=json.dumps(payload or {}, ensure_ascii=False),
                )
                db.add(row)
                await db.commit()
        except Exception as e:
            logger.error(
    "timeline_event_persist_failed",
    session_id=session_id, event_type=event_type, error=str(e),
)

    async def get_timeline_events_async(self, session_id: str) -> t.List[t.Dict[str, t.Any]]:
        """Load timeline events from DB for a session, ordered by creation time."""
        from sqlalchemy import select

        from src.context.database import TimelineEventRow, get_session_factory

        try:
            async with get_session_factory()() as db:
                stmt = (
                    select(TimelineEventRow)
                    .where(TimelineEventRow.session_id == session_id)
                    .order_by(TimelineEventRow.id)
                )
                result = await db.execute(stmt)
                rows = result.scalars().all()

            return [
                {
                    "event_id": row.event_id,
                    "type": row.event_type,
                    "timestamp": row.created_at.isoformat() if row.created_at else "",
                    "payload": json.loads(row.payload_json) if row.payload_json else {},
                    "parent_event_id": row.parent_event_id or "",
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("timeline_events_load_failed", session_id=session_id, error=str(e))
            return []

    # ================================================================
    # Database Persistence (private)
    # ================================================================

    async def _persist_session(self, session: SessionContext) -> None:
        """Write session metadata to database."""
        from src.context.database import SessionRow, get_session_factory

        try:
            async with get_session_factory()() as db:
                row = SessionRow(
                    id=session.session_id,
                    user_id=session.user_id,
                    title=session.title,
                    mode=session.mode.value,
                    execution_style=session.execution_style.value,
                    scenario=session.scenario,
                    status=session.status,
                    message_count=session.message_count,
                    last_summarized_message_count=session.last_summarized_message_count,
                    last_workspace_path=session.workspace_path or "",
                    last_environment=session.environment or "prod",
                )
                db.add(row)
                await db.commit()
                logger.info("session_persisted", session_id=session.session_id)
        except Exception as e:
            logger.error("session_persist_failed", session_id=session.session_id, error=str(e))

    async def _persist_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: t.Optional[t.Dict] = None,
        tool_calls: t.Optional[t.List[t.Dict]] = None,
        tool_call_id: str = "",
    ) -> None:
        """Write a single message to database."""
        from src.context.database import MessageRow, SessionRow, get_session_factory

        try:
            # Merge tool context into metadata_json for DB storage
            merged_metadata = dict(metadata or {})
            if tool_calls:
                merged_metadata["_tool_calls"] = tool_calls
            if tool_call_id:
                merged_metadata["_tool_call_id"] = tool_call_id

            async with get_session_factory()() as db:
                msg_row = MessageRow(
                    session_id=session_id,
                    role=role,
                    content=content,
                    metadata_json=json.dumps(merged_metadata, ensure_ascii=False),
                )
                db.add(msg_row)

                # Update session message_count and updated_at
                from sqlalchemy import update
                stmt = (
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(
                        message_count=SessionRow.message_count + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception as e:
            logger.error("message_persist_failed", session_id=session_id, error=str(e))

    async def _load_session_from_db(self, session_id: str) -> t.Optional[SessionContext]:
        """Load a session and its messages from database into memory."""
        from src.context.database import MessageRow, SessionRow, get_session_factory

        try:
            async with get_session_factory()() as db:
                from sqlalchemy import select
                stmt = select(SessionRow).where(SessionRow.id == session_id)
                result = await db.execute(stmt)
                row = result.scalar_one_or_none()

                if not row:
                    return None

                # Load messages
                msg_stmt = (
                    select(MessageRow)
                    .where(MessageRow.session_id == session_id)
                    .order_by(MessageRow.created_at)
                )
                msg_result = await db.execute(msg_stmt)
                msg_rows = msg_result.scalars().all()

            # Rebuild messages, extracting tool context from metadata_json
            messages: t.List[t.Dict[str, t.Any]] = []
            for m in msg_rows:
                meta = json.loads(m.metadata_json) if m.metadata_json else {}
                msg: t.Dict[str, t.Any] = {
                    "role": m.role,
                    "content": m.content,
                    "metadata": {k: v for k, v in meta.items() if not k.startswith("_")},
                    "timestamp": m.created_at.isoformat() if m.created_at else "",
                }
                # Restore tool context hidden in metadata
                if "_tool_calls" in meta:
                    msg["tool_calls"] = meta["_tool_calls"]
                if "_tool_call_id" in meta:
                    msg["tool_call_id"] = meta["_tool_call_id"]
                messages.append(msg)

            session = SessionContext(
                session_id=row.id,
                user_id=row.user_id,
                title=row.title,
                mode=InteractionMode(row.mode),
                execution_style=ExecutionStyle.from_value(
                    getattr(row, "execution_style", None)
                ),
                status=row.status,
                scenario=row.scenario or "dev",
                message_count=row.message_count,
                messages=messages,
                last_summarized_message_count=row.last_summarized_message_count,
                workspace_path=getattr(row, "last_workspace_path", "") or "",
                environment=getattr(row, "last_environment", "") or "prod",
                created_at=row.created_at,
                last_message_at=row.updated_at,
            )
            if session.workspace_path:
                session.workspace_context = await self._init_workspace_context(
                    session.workspace_path
                )
            logger.info("session_loaded_from_db", session_id=session_id)
            return session

        except Exception as e:
            logger.error("session_load_failed", session_id=session_id, error=str(e))
            return None

    # ================================================================
    # Workspace Context (private)
    # ================================================================

    async def _init_workspace_context(self, workspace_path: str) -> WorkspaceContext:
        """Load ``.agent/`` — rules, recollection documents and configuration.

        Async because the working copy lives in workspace-runtime: this service
        has no volume for it, and reading it locally is what made a
        containerized chat fail on a path it may not even create. The cache is
        what keeps that off the per-message path.

        Nothing is CREATED here any more. Seeding ``.agent/`` wrote into the
        user's repository the first time they said hello, and the defaults it
        seeded already exist in code — a file was never needed to have them.
        The directories now appear when something is actually put in them.
        """
        cached = self._workspace_context_cache.get(workspace_path)
        if cached is not None:
            self._workspace_context_cache.move_to_end(workspace_path)
            return cached

        ctx = WorkspaceContext(workspace_path=workspace_path)
        ctx.rules = await self._load_markdown_dir(workspace_path, "rules")
        ctx.memory_index = "\n\n".join(
            await self._load_markdown_dir(workspace_path, "memory")
        )
        ctx.config = await self._load_agent_config(workspace_path)

        self._cache_workspace_context(ctx)
        logger.info(
            "workspace_context_initialized",
            workspace_path=workspace_path,
            rules=len(ctx.rules),
        )
        return ctx

    async def _load_markdown_dir(
        self, workspace_path: str, subdir: str
    ) -> t.List[str]:
        """Read every ``.md`` file directly under ``.agent/<subdir>``, sorted.

        A workspace with no such directory is the normal case, not a failure:
        rules and recollection documents are opt-in, so an empty list is the
        answer. Only direct children are read — a rules directory is a flat set
        of documents, and recursing would pull in whatever a user happened to
        park below it.
        """
        prefix = f".agent/{subdir}"
        try:
            entries = await workspace_fs.list_entries(
                workspace_path, prefix, pattern="*.md", recursive=False
            )
        except workspace_fs.WorkspaceFileError as exc:
            logger.warning(
                "agent_dir_unreadable", subdir=subdir, error=str(exc)
            )
            return []

        documents: t.List[str] = []
        for entry in sorted(entries, key=lambda item: item.path):
            if entry.is_directory:
                continue
            try:
                document = await workspace_fs.read_text(workspace_path, entry.path)
            except workspace_fs.WorkspaceFileError as exc:
                logger.warning(
                    "agent_doc_load_failed", path=entry.path, error=str(exc)
                )
                continue
            if document.missing or document.binary:
                continue
            content = document.content.strip()
            if content:
                documents.append(content)
        return documents

    async def _load_agent_config(self, workspace_path: str) -> t.Dict[str, t.Any]:
        """Load ``.agent/config.yaml``; an absent or broken file means defaults."""
        import yaml

        try:
            document = await workspace_fs.read_text(
                workspace_path, ".agent/config.yaml"
            )
        except workspace_fs.WorkspaceFileError as exc:
            logger.warning("agent_config_unreadable", error=str(exc))
            return {}
        if document.missing or document.binary or not document.content.strip():
            return {}
        try:
            parsed = yaml.safe_load(document.content)
        except yaml.YAMLError as exc:
            logger.warning("agent_config_load_failed", error=str(exc))
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def load_dynamic_l1_memory(self, workspace_path: str, limit: int = 20) -> str:
        """Query recent L1 memory events from DB for dynamic prompt injection.

        Returns formatted recent-activity text, or empty string if no events found.
        """
        from sqlalchemy import desc, select

        from src.context.database import MemoryEventRow, get_session_factory

        try:
            session_factory = get_session_factory()
            async with session_factory() as db:
                stmt = (
                    select(MemoryEventRow)
                    .where(MemoryEventRow.workspace_path == workspace_path)
                    .order_by(desc(MemoryEventRow.created_at))
                    .limit(limit)
                )
                result = await db.execute(stmt)
                events = result.scalars().all()

            if not events:
                return ""

            lines = ["## Recent Activity (from memory):", ""]
            for ev in events:
                ts = ev.created_at.strftime("%Y-%m-%d %H:%M UTC") if ev.created_at else "?"
                lines.append(f"- [{ev.event_type}] {ev.event_key} ({ts})")
            lines.append("")
            return "\n".join(lines)

        except Exception as e:
            logger.warning(
    "dynamic_l1_memory_load_failed",
    workspace_path=workspace_path, error=str(e),
)
            return ""


# Global singleton
session_manager = SessionManager()
