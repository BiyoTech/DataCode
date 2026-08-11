"""Tests for task board and todo-completion helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.runtime.todo_board import (
    TaskBoard,
    build_todo_continuation_nudge,
    get_task_board,
    has_incomplete_tasks,
    list_incomplete_tasks,
)


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return str(ws)


def test_task_board_create_and_list_incomplete(workspace: str) -> None:
    board = get_task_board(workspace)
    board.create("step one", "pending")
    board.create("step two", "completed")

    incomplete = board.list_incomplete()
    assert len(incomplete) == 1
    assert incomplete[0]["content"] == "step one"


def test_has_incomplete_tasks_false_when_all_done(workspace: str) -> None:
    board = get_task_board(workspace)
    t = board.create("only task", "pending")
    board.update(int(t["id"]), "completed")

    assert has_incomplete_tasks(workspace) is False
    assert list_incomplete_tasks(workspace) == []


def test_build_todo_continuation_nudge_lists_items(workspace: str) -> None:
    board = get_task_board(workspace)
    t = board.create("analyze sales", "in_progress")
    nudge = build_todo_continuation_nudge(board.list_incomplete())

    assert "incomplete task list" in nudge.lower()
    assert "analyze sales" in nudge
    assert f"#{t['id']}" in nudge


def test_session_scoped_task_board_isolated(workspace: str) -> None:
    board_a = get_task_board(workspace, "session-a")
    board_b = get_task_board(workspace, "session-b")
    board_a.create("only in a", "pending")

    assert has_incomplete_tasks(workspace, "session-a") is True
    assert has_incomplete_tasks(workspace, "session-b") is False


def test_task_board_clear_all(workspace: str) -> None:
    board = TaskBoard(root=Path(workspace) / ".agent" / "tasks")
    board.create("a", "pending")
    board.clear_all()
    assert board.list_all() == []
