"""Tool: memory_write_index — write a key event entry to L1 memory index.

Persists a lightweight event record (event_key + event_type + timestamp)
to the memory_events table. This forms the L1 timeline index that gets
automatically injected into agent prompts for context continuity.
"""

import uuid

from agents import RunContextWrapper, function_tool

from src.context.database import MemoryEventRow, get_session_factory
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register(
    "memory_write_index",
    level=1,
    description="Write a key event entry to L1 memory index",
)
@function_tool
async def memory_write_index(
    ctx: RunContextWrapper[ToolContext],
    event_key: str,
    event_type: str = "action",
) -> str:
    """Record a key event to the L1 memory index.

    Use this to capture important milestones, decisions, or actions that
    occurred during the session. These entries form the workspace timeline
    and are automatically surfaced in future conversations.

    Args:
        event_key: Short description of the event (e.g. "Created staging model stg_orders").
        event_type: Category — one of: decision, action, insight, error. Defaults to "action".

    Returns:
        Confirmation with the created event ID.
    """
    workspace_path = ctx.context.workspace_path
    session_id = ctx.context.session_id

    # Validate event_type
    valid_types = {"decision", "action", "insight", "error"}
    if event_type not in valid_types:
        return f"Error: event_type must be one of {valid_types}, got '{event_type}'"

    event_id = str(uuid.uuid4())
    # Truncate event_key to DB column limit (String(512))
    safe_event_key = event_key[:512]
    row = MemoryEventRow(
        id=event_id,
        workspace_path=workspace_path,
        session_id=session_id,
        event_key=safe_event_key,
        event_type=event_type,
    )

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        db_session.add(row)
        await db_session.commit()

    logger.info(
        "memory_event_created",
        event_id=event_id,
        event_key=event_key,
        event_type=event_type,
        session_id=session_id,
    )
    return f"Memory event recorded: [{event_type}] {event_key} (id={event_id})"
