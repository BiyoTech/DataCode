"""write_todo — batch todo planning and progress tracking."""

import json
from typing import Any

from agents import RunContextWrapper, function_tool

from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.context.tool_context import ToolContext
from src.protocol.messages import todo_update as emit_todo_update
from src.runtime.todo_board import get_task_board
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)


@ToolRegistry.register(
    "write_todo",
    level=1,
    description="Plan and track tasks using a structured todo list.",
)
@function_tool
async def write_todo(
    ctx: RunContextWrapper[ToolContext],
    todos: str,
    merge: bool = True,
) -> str:
    """Plan and track tasks using a structured todo list.

    Args:
        todos: A JSON array of todo items. Each item is an object with:
               - id (int|null): Existing task id to update, or null to create new.
               - content (str): Short, actionable description (max 200 chars).
               - status (str): pending | in_progress | completed | deleted.
        merge: When true (default), only the specified items are touched.
               When false, the board is cleared first, then the new items
               become the complete task list.
    """
    board = get_task_board(ctx.context.workspace_path, ctx.context.session_id)

    try:
        items = json.loads(todos) if isinstance(todos, str) else todos
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid todos JSON — {e}"

    if not isinstance(items, list) or not items:
        return "Error: todos must be a non-empty JSON array."

    if not merge:
        board.clear_all()
        logger.info("write_todo_board_cleared", workspace=ctx.context.workspace_path)

    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            results.append({"error": "item must be an object"})
            continue
        try:
            task_id = item.get("id")
            content = item.get("content", "")
            status = item.get("status", "pending")

            if task_id is not None:
                task = board.update(int(task_id), status)
                task["action"] = "updated"
            else:
                task = board.create(content, status)
                task["action"] = "created"
            results.append(task)
        except Exception as exc:
            logger.warning("write_todo_item_failed", item=item, error=str(exc))
            results.append({"error": str(exc), "input": item})

    all_tasks = board.list_all()
    session_id = ctx.context.session_id
    event = emit_todo_update(session_id=session_id, tasks=all_tasks)
    await event_bus.publish(session_id, event)
    await session_manager.add_timeline_event_async(
        session_id=session_id,
        event_type=event.type.value,
        event_id=event.event_id,
        payload=event.payload,
    )
    logger.info(
        "write_todo_completed",
        workspace=ctx.context.workspace_path,
        items_processed=len(results),
        total_tasks=len(all_tasks),
    )
    return _format_summary(results, len(all_tasks))


def _format_summary(results: list[dict[str, Any]], total: int) -> str:
    lines = [f"Todo list updated — {len(results)} item(s) processed, {total} total:"]
    for r in results:
        if "error" in r:
            lines.append(f"  ❌ Error: {r['error']}")
            continue
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "deleted": "[-]"}.get(
            r.get("status", ""), "[?]"
        )
        action = r.get("action", "processed")
        lines.append(f"  {marker} #{r.get('id', '?')}: {r.get('content', '')} ({action})")
    return "\n".join(lines)
