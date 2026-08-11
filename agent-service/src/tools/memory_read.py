"""Tool: memory_read — read memory across the three-layer architecture.

Supports querying L1 (event index), L2 (summaries), and L3 (full conversation
history) with optional time range and keyword filtering. This is the primary
retrieval interface for the memory system.
"""

import typing as t
from datetime import datetime, timezone

from agents import RunContextWrapper, function_tool
from sqlalchemy import select, desc

from src.context.database import (
    MemoryEventRow,
    MemorySummaryRow,
    MessageRow,
    get_session_factory,
)
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register(
    "memory_read",
    level=1,
    description="Read memory across layers (L1 index, L2 summary, L3 history)",
)
@function_tool
async def memory_read(
    ctx: RunContextWrapper[ToolContext],
    layer: str = "L1",
    event_id: str = "",
    session_id: str = "",
    keyword: str = "",
    limit: int = 20,
) -> str:
    """Read memory from the three-layer memory architecture.

    - L1: Returns recent event index entries (lightweight timeline).
    - L2: Returns the structured summary for a specific event (requires event_id).
    - L3: Returns full conversation history for a session (requires session_id).

    Args:
        layer: Memory layer to query — "L1", "L2", or "L3". Defaults to "L1".
        event_id: (L2 only) The event ID to retrieve summary for.
        session_id: (L3 only) The session ID to retrieve full history for.
        keyword: Optional keyword filter for L1 event_key search.
        limit: Maximum number of results to return. Defaults to 20.

    Returns:
        Formatted memory content from the requested layer.
    """
    workspace_path = ctx.context.workspace_path
    layer = layer.upper()

    if layer == "L1":
        return await _read_l1(workspace_path, keyword, limit)
    elif layer == "L2":
        if not event_id:
            return "Error: event_id is required for L2 layer query."
        return await _read_l2(event_id)
    elif layer == "L3":
        target_session = session_id or ctx.context.session_id
        return await _read_l3(target_session, limit)
    else:
        return f"Error: Invalid layer '{layer}'. Must be L1, L2, or L3."


async def _read_l1(workspace_path: str, keyword: str, limit: int) -> str:
    """Query L1 event index for the workspace."""
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        stmt = (
            select(MemoryEventRow)
            .where(MemoryEventRow.workspace_path == workspace_path)
        )
        if keyword:
            stmt = stmt.where(MemoryEventRow.event_key.contains(keyword))
        stmt = stmt.order_by(desc(MemoryEventRow.created_at)).limit(limit)

        result = await db_session.execute(stmt)
        events = result.scalars().all()

    if not events:
        return "No memory events found for this workspace."

    lines = ["## L1 Memory Index (Recent Events)\n"]
    for ev in events:
        ts = ev.created_at.strftime("%Y-%m-%d %H:%M UTC") if ev.created_at else "?"
        lines.append(f"- [{ev.event_type}] {ev.event_key} ({ts}) [id={ev.id}]")

    logger.info("memory_read_l1", workspace_path=workspace_path, count=len(events))
    return "\n".join(lines)


async def _read_l2(event_id: str) -> str:
    """Query L2 summaries for a specific event."""
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        stmt = (
            select(MemorySummaryRow)
            .where(MemorySummaryRow.event_id == event_id)
            .order_by(desc(MemorySummaryRow.created_at))
        )
        result = await db_session.execute(stmt)
        summaries = result.scalars().all()

    if not summaries:
        return f"No summaries found for event {event_id}."

    lines = [f"## L2 Memory Summary (Event: {event_id})\n"]
    for s in summaries:
        ts = s.created_at.strftime("%Y-%m-%d %H:%M UTC") if s.created_at else "?"
        lines.append(f"### Summary ({ts})")
        lines.append(s.summary)
        if s.context_json and s.context_json != "{}":
            lines.append(f"\n**Context**: {s.context_json}")
        lines.append("")

    logger.info("memory_read_l2", event_id=event_id, count=len(summaries))
    return "\n".join(lines)


async def _read_l3(session_id: str, limit: int) -> str:
    """Query L3 full conversation history — returns most recent messages."""
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        # Fetch most recent messages (descending), then reverse to chronological order
        stmt = (
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(desc(MessageRow.id))
            .limit(limit)
        )
        result = await db_session.execute(stmt)
        messages = list(reversed(result.scalars().all()))

    if not messages:
        return f"No conversation history found for session {session_id}."

    lines = [f"## L3 Conversation History (Session: {session_id})\n"]
    for msg in messages:
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M UTC") if msg.created_at else "?"
        content_preview = msg.content[:500] if msg.content else "(empty)"
        lines.append(f"**[{msg.role}]** ({ts}): {content_preview}")
        lines.append("")

    logger.info("memory_read_l3", session_id=session_id, count=len(messages))
    return "\n".join(lines)
