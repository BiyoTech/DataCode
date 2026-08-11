"""Tool: memory_write_summary — write a structured summary for an L1 event to L2.

Persists a detailed summary (description + context JSON) linked to an existing
L1 memory event. This provides the mid-level recall layer that agents access
on-demand when they need more context about a specific event.
"""

import json
import uuid
import typing as t

from agents import RunContextWrapper, function_tool

from src.context.database import MemorySummaryRow, get_session_factory
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register(
    "memory_write_summary",
    level=1,
    description="Write a structured summary for an L1 event to L2 memory",
)
@function_tool
async def memory_write_summary(
    ctx: RunContextWrapper[ToolContext],
    event_id: str,
    summary: str,
    context: str = "{}",
) -> str:
    """Write a structured summary for a previously recorded L1 event.

    The summary provides richer context about what happened, why decisions
    were made, and what the outcomes were. This is the L2 layer accessed
    on-demand for deeper recall.

    Args:
        event_id: The UUID of the L1 memory event to attach this summary to.
        summary: A concise but informative description of the event context and outcome.
        context: JSON string with structured metadata (e.g. related files, decision rationale).

    Returns:
        Confirmation with the created summary ID.
    """
    workspace_path = ctx.context.workspace_path

    # Validate context is valid JSON
    try:
        json.loads(context)
    except json.JSONDecodeError:
        return "Error: 'context' must be a valid JSON string."

    summary_id = str(uuid.uuid4())
    row = MemorySummaryRow(
        id=summary_id,
        event_id=event_id,
        workspace_path=workspace_path,
        summary=summary,
        context_json=context,
    )

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        db_session.add(row)
        await db_session.commit()

    logger.info(
        "memory_summary_created",
        summary_id=summary_id,
        event_id=event_id,
    )
    return f"Memory summary recorded for event {event_id} (summary_id={summary_id})"
