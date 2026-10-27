"""Session task board — todo state for write_todo and goal-run continuation.

Held in THIS PROCESS, deliberately not in the working copy. Two reasons, both
structural:

* agent-service owns no working copy. The copy belongs to the
  workspace-runtime replica that holds its lease, and in every container
  deployment the agent is given no volume for it at all — which is how a chat
  used to die with ``[Errno 13] Permission denied: '/data'``: the board's
  ``mkdir(parents=True)`` walked up to a root the container may not create.
* Even where the path happens to be reachable (host dev), writing there makes
  agent-service a SECOND writer inside a leased Git worktree, which is exactly
  what the lease exists to prevent.

Lifetime is the process, and that is enough: the board is run-control state,
read only by the goal loop while a run is in flight. What the USER sees is the
``todo_list`` timeline event, which ``write_todo`` persists — so a restart
costs the continuation nudge, never the visible task list.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

_ALLOWED_STATUS = {"pending", "in_progress", "completed", "deleted"}
_INCOMPLETE_STATUS = {"pending", "in_progress"}

# Boards are keyed per (workspace, session) and nothing tells this module when a
# session ends, so the registry is bounded and evicts the least recently used —
# an unbounded dict here would be a slow leak in a long-running service. A board
# is a handful of short strings, so the cap can be generous.
MAX_BOARDS = 500


class TaskBoard:
    """Thread-safe todo storage for one session.

    Thread-safe rather than async-locked: the writer is an async tool and the
    reader is the goal loop, but both call in from threads the SDK owns, and a
    ``threading.Lock`` is the only primitive correct for both.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Insertion-ordered: list_all() must reproduce the order the agent
        # declared its plan in, which is what the UI card renders.
        self._tasks: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
        self._next = 1

    def create(self, content: str, status: str = "pending") -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > 200:
            raise ValueError("content too long (max 200 chars)")
        with self._lock:
            task = {
                "id": self._next,
                "content": content,
                "status": status if status in _ALLOWED_STATUS else "pending",
            }
            self._tasks[task["id"]] = task
            self._next += 1
        return dict(task)

    def update(self, task_id: int, status: str) -> dict[str, Any]:
        normalized = status.lower()
        if normalized not in _ALLOWED_STATUS:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            task = self._tasks.get(int(task_id))
            if task is None:
                raise ValueError(f"task {task_id} not found")
            task["status"] = normalized
            if normalized == "deleted":
                # Deleted items must disappear from the incomplete set AND from
                # the snapshot; keeping them would re-nudge forever.
                self._tasks.pop(int(task_id), None)
            return dict(task)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(task) for task in self._tasks.values()]

    def list_incomplete(self) -> list[dict[str, Any]]:
        return [
            task for task in self.list_all()
            if task.get("status", "pending") in _INCOMPLETE_STATUS
        ]

    def clear_all(self) -> None:
        with self._lock:
            self._tasks.clear()


_boards: "OrderedDict[str, TaskBoard]" = OrderedDict()
_boards_lock = threading.Lock()


def get_task_board(workspace_path: str, session_id: str = "") -> TaskBoard:
    """The board for one working copy + session, created on first use."""
    key = f"{workspace_path}\x00{session_id}"
    with _boards_lock:
        board = _boards.get(key)
        if board is None:
            board = TaskBoard()
            _boards[key] = board
        else:
            _boards.move_to_end(key)
        while len(_boards) > MAX_BOARDS:
            _boards.popitem(last=False)
        return board


# NOTE: Read and write paths MUST resolve to the SAME board. ``write_todo`` only
# ever touches the session-scoped board, so the incomplete-task checks below must
# be session-scoped too. A previous "legacy workspace-level board" fallback broke
# this symmetry: orphaned incomplete tasks in the workspace-level board (left over
# from other sessions) could never be completed or cleared by ``write_todo``,
# which caused perpetual false TODO_CONTINUATION and "task not found" update
# failures. We therefore only consult the current session's board.


def has_incomplete_tasks(workspace_path: str, session_id: str = "") -> bool:
    if not workspace_path:
        return False
    return bool(get_task_board(workspace_path, session_id).list_incomplete())


def list_incomplete_tasks(workspace_path: str, session_id: str = "") -> list[dict[str, Any]]:
    if not workspace_path:
        return []
    return get_task_board(workspace_path, session_id).list_incomplete()


def build_todo_continuation_nudge(incomplete: list[dict[str, Any]]) -> str:
    lines = [
        "[System — incomplete task list]",
        (
            "Your todo list still has unfinished items. Continue executing until ALL "
            "items are completed or explicitly removed (status=deleted). Do not stop "
            "with a summary while work remains."
        ),
        (
            "IMPORTANT: Do NOT restate, re-explain, or paraphrase your previous plan, "
            "analysis, or SQL. Skip narration and take the NEXT concrete action "
            "immediately (call a tool). If an approach has already failed once, do "
            "NOT retry it unchanged — either fix the root cause or mark the item "
            "as deleted and move on."
        ),
        "",
        "Remaining items:",
    ]
    for task in incomplete:
        tid = task.get("id", "?")
        status = task.get("status", "pending")
        content = task.get("content", "")
        lines.append(f"  - #{tid} [{status}]: {content}")
    return "\n".join(lines)
