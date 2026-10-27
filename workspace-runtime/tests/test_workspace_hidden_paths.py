"""What a working copy hides, and what Git must never track.

Three surfaces have to agree about the same tree — the explorer, the flat index
behind search and @-mentions, and the ``.gitignore`` written at creation time —
and their disagreements are silent: a directory missing from one of them is not
an error anywhere, it is just an artefact that shows up in a listing, a search
result or a commit.

The concrete defect this module was written for: SQLMesh's personal directory
appeared at the ROOT of every working copy. Its ``user.yaml`` is a telemetry id
written even with telemetry disabled — but the same directory also holds an
optional personal ``config.yaml`` that SQLMesh merges OVER the project's, so
its location is a configuration-override surface, not just noise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.workspace_layout import (  # noqa: E402
    PLATFORM_STATE_DIR,
    RENDERED_PROJECT_DIR,
    SQLMESH_HOME_DIR,
    TOOL_HOME_DIR,
)

from src.files import ops as file_ops  # noqa: E402
from src.files.layout import WORKSPACE_GITIGNORE  # noqa: E402
from src.git.ops import GitOperations  # noqa: E402


@pytest.fixture()
def ws(tmp_path) -> str:
    """A workspace root with one user file and the artefacts we must hide."""
    root = tmp_path / "ws"
    (root / "sql-warehouse" / "models").mkdir(parents=True)
    (root / "sql-warehouse" / "models" / "orders.sql").write_text(
        "select 1", encoding="utf-8"
    )
    (root / SQLMESH_HOME_DIR).mkdir(parents=True)
    (root / SQLMESH_HOME_DIR / "user.yaml").write_text("id: abc\n", encoding="utf-8")
    (root / RENDERED_PROJECT_DIR / "models").mkdir(parents=True)
    (root / RENDERED_PROJECT_DIR / "models" / "orders.sql").write_text(
        "select 1", encoding="utf-8"
    )
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "thing.py").write_text("x = 1", encoding="utf-8")
    (root / ".agent" / "rules").mkdir(parents=True)
    (root / ".agent" / "rules" / "style.md").write_text("# rules", encoding="utf-8")
    (root / ".agent" / "tasks").mkdir(parents=True)
    (root / ".agent" / "tasks" / "board.json").write_text("{}", encoding="utf-8")
    return str(root)


# ── the explorer tree ────────────────────────────────────────────────────


def test_platform_state_never_reaches_the_explorer(ws):
    names = {f.path for f in file_ops.list_files(ws)}

    assert PLATFORM_STATE_DIR not in names
    assert "sql-warehouse" in names


def test_machine_written_trees_stay_browsable(ws):
    """Hidden from search is not hidden from the user: a virtual environment
    is theirs to open, it simply must not drown the index."""
    names = {f.path for f in file_ops.list_files(ws)}

    assert ".venv" in names
    assert ".agent" in names


# ── the flat index behind search and @-mentions ──────────────────────────


def test_the_index_holds_project_files_only(ws):
    names = {f.path for f in file_ops.list_files(ws, recursive=True)}

    assert "sql-warehouse/models/orders.sql" in names
    assert os.path.join(".agent", "rules", "style.md") in names
    assert not [n for n in names if n.startswith(PLATFORM_STATE_DIR)]
    assert not [n for n in names if n.startswith(".venv")]
    assert not [n for n in names if n.startswith(os.path.join(".agent", "tasks"))]


def test_listing_inside_a_hidden_tree_yields_nothing(ws):
    """The rendered project is a second copy of the user's own models.

    Serving it would answer "why do I have two projects?" with a listing.
    """
    assert file_ops.list_files(ws, directory=RENDERED_PROJECT_DIR) == []


# ── the ignore file ──────────────────────────────────────────────────────


def test_gitignore_covers_the_platform_state_directory():
    """The rendered project has always been written here, and the constant
    that names it claimed it was ignored. It was not."""
    assert f"\n/{PLATFORM_STATE_DIR}/\n" in WORKSPACE_GITIGNORE


def test_gitignore_anchors_platform_directories_at_the_root():
    """A leading slash, so a user directory called ``logs`` deeper in the tree
    is theirs."""
    for entry in (f"/{PLATFORM_STATE_DIR}/", "/logs/", "/.sqlmesh/"):
        assert f"\n{entry}\n" in WORKSPACE_GITIGNORE, entry


def test_gitignore_covers_caches_at_every_depth():
    """No leading slash: gitignore then matches the name in ANY directory."""
    for entry in ("__pycache__/", ".venv/", ".cache/", "node_modules/"):
        assert f"\n{entry}\n" in WORKSPACE_GITIGNORE, entry


def test_gitignore_keeps_team_shared_agent_conventions_optional():
    """Listed but commented out, so sharing rules is a decision, not a
    default."""
    assert "\n# /.agent/rules/\n" in WORKSPACE_GITIGNORE
    assert "\n/.agent/tasks/\n" in WORKSPACE_GITIGNORE


# ── every vehicle agrees on where a tool's home is ───────────────────────


def test_every_container_vehicle_pins_home():
    """A container's HOME comes from its IMAGE unless the Pod spec says
    otherwise, and the image's choice (``/nonexistent`` for the unprivileged
    uid, on a read-only root filesystem) is not writable.

    Asserted on the source because building these specs needs a cluster
    client; what must not regress is that no vehicle is left out.
    """
    import inspect

    from src.executor import container as executor_container
    from src.tasks import warm_pool
    from src.tasks.backends import kubernetes

    for func in (
        kubernetes.KubernetesBackend._create_job,
        executor_container.WarmSandboxBackend._create_pod,
        warm_pool.WarmPool._create_pod,
    ):
        source = inspect.getsource(func)
        assert 'name="HOME"' in source, func.__qualname__
        assert "tool_home(" in source, func.__qualname__


def test_the_executors_restated_constants_match_the_shared_ones():
    """``executor/server.py`` must start from a BARE interpreter.

    It therefore cannot import the shared package and restates two paths by
    hand. Restating is acceptable; drifting is not — a sandbox writing its
    SQLMesh home somewhere the ``.gitignore`` does not cover puts a telemetry
    id in the user's next commit.
    """
    from src.executor import server

    assert server._TOOL_HOME_DIR == TOOL_HOME_DIR
    assert server._SQLMESH_HOME_DIR == SQLMESH_HOME_DIR


# ── end to end: what a first commit actually contains ────────────────────


def test_first_commit_tracks_project_files_only(tmp_path):
    """Rules written after ``git add`` would be too late: the file is tracked.

    Runs real Git so the assertion is about gitignore SEMANTICS (anchoring,
    trailing slashes, ordering) rather than about string membership.
    """
    if shutil.which("git") is None:  # pragma: no cover - dev machines have git
        pytest.skip("git is not installed")

    workspace = tmp_path / "ws"
    (workspace / "sql-warehouse" / "models").mkdir(parents=True)
    (workspace / "sql-warehouse" / "models" / "orders.sql").write_text(
        "select 1", encoding="utf-8"
    )
    (workspace / SQLMESH_HOME_DIR).mkdir(parents=True)
    (workspace / SQLMESH_HOME_DIR / "user.yaml").write_text(
        "id: abc\n", encoding="utf-8"
    )
    (workspace / RENDERED_PROJECT_DIR).mkdir(parents=True)
    (workspace / RENDERED_PROJECT_DIR / "orders.sql").write_text(
        "select 1", encoding="utf-8"
    )
    legacy = workspace / ".sqlmesh"
    legacy.mkdir()
    (legacy / "user.yaml").write_text("id: def\n", encoding="utf-8")
    (workspace / ".agent" / "tasks").mkdir(parents=True)
    (workspace / ".agent" / "tasks" / "board.json").write_text("{}", encoding="utf-8")
    (workspace / ".agent" / "rules").mkdir(parents=True)
    (workspace / ".agent" / "rules" / "style.md").write_text("# r", encoding="utf-8")
    warehouse_db = workspace / "sql-warehouse" / "data"
    warehouse_db.mkdir()
    (warehouse_db / "local.duckdb").write_bytes(b"\x00")

    assert GitOperations.init(str(workspace)) is True

    tracked = subprocess.run(
        ["git", "-C", str(workspace), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert "sql-warehouse/models/orders.sql" in tracked
    # Team conventions are shareable: the rule for them is commented out.
    assert ".agent/rules/style.md" in tracked

    for unwanted in (PLATFORM_STATE_DIR, ".sqlmesh/", ".agent/tasks/", ".duckdb"):
        assert not [p for p in tracked if unwanted in p], unwanted
