"""What the agent's file tools may see in a working copy.

Two defects motivated these tests, and both were invisible from inside the
tools:

1. ``.agent/`` was excluded wholesale. The design subagent is instructed that
   its ONLY artefact is ``.agent/design/<name>.md`` — and then could neither
   list nor search what it had just written. Rules, memory notes and custom
   skills were equally invisible, while the runtime's own explorer showed them
   all: the same copy looked different depending on who was looking.

2. Exclusions were matched against the ABSOLUTE path's components. The
   platform's default workspace root is ``~/.datacode/workspaces/...``, so
   adding the platform state directory to the list would have hidden every file
   of every workspace, everywhere, with no error to notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools._file_utils import (  # noqa: E402
    is_hidden_entry,
    iter_visible_entries,
)


def _workspace(root: Path) -> Path:
    """A copy holding user content next to every kind of artefact."""
    (root / "sql-warehouse" / "models").mkdir(parents=True)
    (root / "sql-warehouse" / "models" / "orders.sql").write_text("select 1")
    (root / ".agent" / "design").mkdir(parents=True)
    (root / ".agent" / "design" / "warehouse.md").write_text("# design")
    (root / ".agent" / "rules").mkdir(parents=True)
    (root / ".agent" / "rules" / "style.md").write_text("# rules")
    (root / ".agent" / "tasks").mkdir(parents=True)
    (root / ".agent" / "tasks" / "board.json").write_text("{}")
    (root / ".datacode" / "rendered" / "models").mkdir(parents=True)
    (root / ".datacode" / "rendered" / "models" / "orders.sql").write_text("select 1")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "thing.py").write_text("x = 1")
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "ab").write_bytes(b"\x00")
    return root


def _visible(root: Path) -> set[str]:
    return {
        entry.relative_to(root).as_posix()
        for entry in iter_visible_entries(root, root, recursive=True)
    }


# ── .agent: authored content vs per-user state ───────────────────────────


def test_the_design_subagents_own_artefact_is_visible(tmp_path):
    root = _workspace(tmp_path / "ws")

    assert ".agent/design/warehouse.md" in _visible(root)


def test_agent_rules_are_visible(tmp_path):
    root = _workspace(tmp_path / "ws")

    assert ".agent/rules/style.md" in _visible(root)


def test_agent_runtime_state_is_not(tmp_path):
    """Rewritten every turn and reachable through the todo board tool: in a
    search result it is pure noise."""
    root = _workspace(tmp_path / "ws")

    assert ".agent/tasks/board.json" not in _visible(root)


# ── machine-written trees ────────────────────────────────────────────────


def test_platform_state_and_dependency_trees_are_skipped(tmp_path):
    root = _workspace(tmp_path / "ws")
    visible = _visible(root)

    assert not [p for p in visible if p.startswith(".datacode")]
    assert not [p for p in visible if p.startswith(".venv")]
    assert not [p for p in visible if p.startswith(".git/")]
    assert "sql-warehouse/models/orders.sql" in visible


def test_excluded_trees_are_pruned_not_filtered(tmp_path, monkeypatch):
    """Skipping a directory must skip its subtree.

    ``rglob`` enumerated every file of every virtual environment before
    discarding them one at a time, which on a real copy is most of the walk.
    """
    root = _workspace(tmp_path / "ws")
    deep = root / ".venv" / "lib" / "site-packages" / "pkg"
    deep.mkdir(parents=True)
    (deep / "mod.py").write_text("y = 2")

    walked: list[str] = []
    import os as _os

    real_walk = _os.walk

    def _tracking_walk(top, *args, **kwargs):
        for entry in real_walk(top, *args, **kwargs):
            walked.append(str(entry[0]))
            yield entry

    monkeypatch.setattr("src.tools._file_utils.os.walk", _tracking_walk)

    _visible(root)

    assert not [p for p in walked if ".venv" in p]


# ── relative matching ────────────────────────────────────────────────────


def test_a_workspace_below_the_platform_directory_is_fully_visible(tmp_path):
    """The dev default root is ``~/.datacode/workspaces/<tenant>/...``.

    Judging absolute paths would match ``.datacode`` above the workspace and
    return an empty listing for every project on the machine.
    """
    root = _workspace(tmp_path / ".datacode" / "workspaces" / "t1" / "ws")

    assert "sql-warehouse/models/orders.sql" in _visible(root)


def test_an_entry_outside_the_workspace_is_refused(tmp_path):
    root = _workspace(tmp_path / "ws")

    assert is_hidden_entry(root, tmp_path / "elsewhere" / "secret.sql")


# ── non-recursive listing uses the same policy ───────────────────────────


def test_flat_listing_hides_the_same_trees(tmp_path):
    root = _workspace(tmp_path / "ws")

    names = {
        entry.name for entry in iter_visible_entries(root, root, recursive=False)
    }

    assert "sql-warehouse" in names
    assert ".agent" in names
    assert ".git" not in names
    assert ".datacode" not in names
    assert ".venv" not in names
