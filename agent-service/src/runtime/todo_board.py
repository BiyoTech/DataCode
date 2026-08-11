"""Workspace task board — persistence for write_todo and goal-run completion checks."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ALLOWED_STATUS = {"pending", "in_progress", "completed", "deleted"}
_INCOMPLETE_STATUS = {"pending", "in_progress"}


@dataclass
class TaskBoard:
    """Thread-safe, file-based task storage (one JSON file per task)."""

    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _next_id(self) -> int:
        ids: list[int] = []
        for fp in self.root.glob("task_*.json"):
            try:
                ids.append(int(fp.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return (max(ids) if ids else 0) + 1

    def _path(self, task_id: int) -> Path:
        return self.root / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict[str, Any]:
        fp = self._path(task_id)
        if not fp.exists():
            raise ValueError(f"task {task_id} not found")
        return json.loads(fp.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        fp = self._path(int(data["id"]))
        tmp = fp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(fp)

    def create(self, content: str, status: str = "pending") -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > 200:
            raise ValueError("content too long (max 200 chars)")
        with self._lock:
            task = {
                "id": self._next_id(),
                "content": content,
                "status": status if status in _ALLOWED_STATUS else "pending",
            }
            self._save(task)
        return task

    def update(self, task_id: int, status: str) -> dict[str, Any]:
        normalized = status.lower()
        if normalized not in _ALLOWED_STATUS:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            task = self._load(task_id)
            task["status"] = normalized
            if normalized == "deleted":
                self._path(task_id).unlink(missing_ok=True)
            else:
                self._save(task)
        return task

    def list_all(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for fp in sorted(self.root.glob("task_*.json")):
            try:
                items.append(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
        return items

    def list_incomplete(self) -> list[dict[str, Any]]:
        return [
            t for t in self.list_all()
            if t.get("status", "pending") in _INCOMPLETE_STATUS
        ]

    def clear_all(self) -> None:
        with self._lock:
            for fp in self.root.glob("task_*.json"):
                fp.unlink(missing_ok=True)


_boards: dict[str, TaskBoard] = {}
_boards_lock = threading.Lock()


def task_board_root(workspace_path: str, session_id: str = "") -> Path:
    """Resolve the on-disk directory for a task board."""
    base = Path(workspace_path).resolve() / ".agent" / "tasks"
    if session_id:
        return base / session_id
    return base


def get_task_board(workspace_path: str, session_id: str = "") -> TaskBoard:
    root = task_board_root(workspace_path, session_id)
    key = str(root)
    with _boards_lock:
        if key not in _boards:
            _boards[key] = TaskBoard(root=root)
        return _boards[key]


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
