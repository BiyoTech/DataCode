"""Background task helpers.

asyncio only keeps *weak* references to tasks created via ``create_task``.
A fire-and-forget task whose reference is not retained elsewhere can be garbage
collected mid-execution, silently cancelling it. Unhandled exceptions in such
tasks are also swallowed until GC (surfacing only as a late
"Task exception was never retrieved" warning).

``spawn_background_task`` fixes both problems:
  - keeps a strong reference in a module-level set until the task completes;
  - attaches a done callback that logs any exception and drops the reference.
"""

import asyncio
import typing as t

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Strong references to in-flight background tasks. Prevents premature GC.
_background_tasks: t.Set[asyncio.Task] = set()


def spawn_background_task(
    coro: t.Coroutine[t.Any, t.Any, t.Any],
    *,
    name: str,
) -> asyncio.Task:
    """Schedule *coro* as a tracked background task.

    The task reference is held until completion, and any exception is logged
    (CancelledError is expected on shutdown/cancel and is not treated as error).

    Args:
        coro: The coroutine to run.
        name: A short label used for logging and task naming.

    Returns:
        The created asyncio.Task (also tracked internally).
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    """Done callback: drop the strong reference and log any failure."""
    _background_tasks.discard(task)
    if task.cancelled():
        logger.info("background_task_cancelled", task=task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background_task_failed",
            task=task.get_name(),
            error=str(exc),
            exc_info=exc,
        )
