"""Tests for the session task board and the todo-completion helpers.

The board is process state on purpose (see ``src.runtime.todo_board``), so the
assertions here are about isolation, ordering and the absence of any filesystem
dependency — the last one is what kept a containerized chat from running at all.
"""

from __future__ import annotations

import pytest

from src.runtime import todo_board
from src.runtime.todo_board import (
    TaskBoard,
    build_todo_continuation_nudge,
    get_task_board,
    has_incomplete_tasks,
    list_incomplete_tasks,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test gets an empty board registry (it is module state)."""
    todo_board._boards.clear()
    yield
    todo_board._boards.clear()


@pytest.fixture
def workspace() -> str:
    # A path this process cannot write to: the board must never touch it.
    return "/data/workspaces/t1/ws-t1-shop/u1/envs/dev"


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
    get_task_board(workspace, "session-b")
    board_a.create("only in a", "pending")

    assert has_incomplete_tasks(workspace, "session-a") is True
    assert has_incomplete_tasks(workspace, "session-b") is False


def test_task_board_clear_all() -> None:
    board = TaskBoard()
    board.create("a", "pending")
    board.clear_all()
    assert board.list_all() == []


def test_deleted_task_leaves_the_board(workspace: str) -> None:
    # Otherwise the goal loop would nudge forever about an item the agent
    # explicitly abandoned.
    board = get_task_board(workspace)
    t = board.create("abandon me", "pending")
    board.update(int(t["id"]), "deleted")

    assert board.list_all() == []
    assert has_incomplete_tasks(workspace) is False


def test_updating_an_unknown_task_is_reported(workspace: str) -> None:
    with pytest.raises(ValueError, match="not found"):
        get_task_board(workspace).update(42, "completed")


def test_list_all_keeps_the_declared_order(workspace: str) -> None:
    # The UI card renders this order; sorting by id string ("task_10" < "task_2")
    # is the classic way to lose it.
    board = get_task_board(workspace)
    for i in range(1, 12):
        board.create(f"step {i}", "pending")

    assert [t["content"] for t in board.list_all()] == [
        f"step {i}" for i in range(1, 12)
    ]


def test_board_registry_is_bounded(workspace: str, monkeypatch) -> None:
    # Nothing tells this module when a session ends, so an unbounded registry
    # would be a slow leak in a long-running service.
    monkeypatch.setattr(todo_board, "MAX_BOARDS", 3)
    for i in range(5):
        get_task_board(workspace, f"session-{i}")

    assert len(todo_board._boards) == 3


def test_board_never_touches_the_filesystem(tmp_path, monkeypatch) -> None:
    """The regression: agent-service owns no working copy.

    A board that creates directories dies with ``Permission denied: '/data'``
    in every container deployment, and adds a second writer to a leased Git
    worktree everywhere else.
    """
    import os

    def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"the task board touched the filesystem: {args!r}")

    monkeypatch.setattr(os, "mkdir", _forbidden)
    monkeypatch.setattr(os, "makedirs", _forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", _forbidden)
    monkeypatch.setattr("pathlib.Path.write_text", _forbidden)

    board = get_task_board("/data/workspaces/t1/ws-t1-shop/u1/envs/dev", "s1")
    task = board.create("no io please", "in_progress")
    board.update(int(task["id"]), "completed")
    board.list_all()
    board.clear_all()
