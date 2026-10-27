"""Render-phase tests (task-runner side of the write split).

Focused on the properties the phase-2 sandbox depends on: the output must be
SQL-only, must be complete, and must stay inside the working copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import handlers
from src.sqlmesh_ops import (
    RENDERED_PROJECT_DIR,
    SqlMeshOpError,
    find_python_code,
    op_render_project,
    resolve_sqlmesh_root,
)


class _Query:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    def sql(self, dialect: str = "", pretty: bool = False) -> str:
        return self._sql


class _Kind:
    name = "FULL"


class _Model:
    def __init__(self, name: str, sql: str, dialect: str = "duckdb") -> None:
        self.name = name
        self.dialect = dialect
        self.kind = _Kind()
        self._sql = sql

    def render_query(self):
        return _Query(self._sql)


class _Context:
    """A Context stand-in: the real one needs a warehouse and an engine."""

    default_dialect = "duckdb"

    def __init__(self, models: dict, failing: str = "") -> None:
        self.models = models
        self._failing = failing

    def render(self, name: str):
        if name == self._failing:
            raise RuntimeError("this model cannot be rendered")
        return _Query(self.models[name]._sql)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A working copy whose project contains a Python model and a macro."""
    warehouse = tmp_path / "sql-warehouse"
    (warehouse / "models").mkdir(parents=True)
    (warehouse / "macros").mkdir()
    (warehouse / "audits").mkdir()
    (warehouse / "config.yaml").write_text("gateways: {}\n", encoding="utf-8")
    (warehouse / "models" / "orders.sql").write_text("SELECT 1", encoding="utf-8")
    (warehouse / "models" / "derived.py").write_text("# python model", encoding="utf-8")
    (warehouse / "macros" / "helpers.py").write_text("# macro", encoding="utf-8")
    (warehouse / "audits" / "checks.sql").write_text("SELECT 1", encoding="utf-8")
    return tmp_path


@pytest.fixture
def patched_context(monkeypatch):
    """Replace Context loading with a stand-in; returns a setter for the models."""
    holder: dict = {}

    def fake_load(root: str, gateway: str = ""):
        holder["root"] = root
        return holder["context"]

    monkeypatch.setattr("src.sqlmesh_ops.load_context", fake_load)
    return holder


class TestRenderProject:
    def test_the_output_is_sql_only(self, project, patched_context):
        """The property phase 2's writable account depends on."""
        patched_context["context"] = _Context(
            {
                "db.orders": _Model("db.orders", "SELECT 1 AS id"),
                "db.derived": _Model("db.derived", "SELECT 2 AS id"),
            }
        )

        result = op_render_project(str(project), {})

        assert result["python_code_remaining"] == []
        rendered_root = project / RENDERED_PROJECT_DIR
        assert find_python_code(str(rendered_root)) == []

    def test_every_model_is_written_as_sql(self, project, patched_context):
        patched_context["context"] = _Context(
            {
                "db.orders": _Model("db.orders", "SELECT 1 AS id"),
                "db.derived": _Model("db.derived", "SELECT 2 AS id"),
            }
        )

        result = op_render_project(str(project), {})

        assert result["model_count"] == 2
        models_dir = project / RENDERED_PROJECT_DIR / "models"
        written = sorted(p.name for p in models_dir.iterdir())
        assert written == ["db.derived.sql", "db.orders.sql"]
        body = (models_dir / "db.orders.sql").read_text(encoding="utf-8")
        assert body.startswith("MODEL (")
        assert "name db.orders" in body
        assert "SELECT 1 AS id" in body

    def test_configuration_and_audits_are_copied_verbatim(self, project, patched_context):
        """Rewriting them could change behaviour between the phases."""
        patched_context["context"] = _Context(
            {"db.orders": _Model("db.orders", "SELECT 1")}
        )

        result = op_render_project(str(project), {})

        rendered_root = project / RENDERED_PROJECT_DIR
        assert (rendered_root / "config.yaml").read_text() == "gateways: {}\n"
        assert (rendered_root / "audits" / "checks.sql").is_file()
        assert "config.yaml" in result["files_copied"]

    def test_macros_are_not_copied(self, project, patched_context):
        """They are the executable half; the rendering replaces them."""
        patched_context["context"] = _Context(
            {"db.orders": _Model("db.orders", "SELECT 1")}
        )

        op_render_project(str(project), {})

        assert not (project / RENDERED_PROJECT_DIR / "macros").exists()

    def test_a_partial_rendering_is_discarded(self, project, patched_context):
        """Applying an incomplete project would look like data disappearing."""
        patched_context["context"] = _Context(
            {
                "db.orders": _Model("db.orders", "SELECT 1"),
                "db.broken": _Model("db.broken", "SELECT 2"),
            },
            failing="db.broken",
        )

        with pytest.raises(SqlMeshOpError, match="incomplete project"):
            op_render_project(str(project), {})

        # Nothing usable is left behind for phase 2 to pick up.
        rendered_root = project / RENDERED_PROJECT_DIR
        assert not any(rendered_root.rglob("*.sql"))

    def test_a_previous_rendering_is_replaced_not_merged(self, project, patched_context):
        """A stale model file would be applied as if it were current."""
        stale = project / RENDERED_PROJECT_DIR / "models"
        stale.mkdir(parents=True)
        (stale / "db.gone.sql").write_text("SELECT 0", encoding="utf-8")
        patched_context["context"] = _Context(
            {"db.orders": _Model("db.orders", "SELECT 1")}
        )

        op_render_project(str(project), {})

        assert not (stale / "db.gone.sql").exists()

    def test_the_reported_path_is_relative_to_the_working_copy(
        self, project, patched_context
    ):
        """Phase 2 runs in another sandbox with a different mount point."""
        patched_context["context"] = _Context(
            {"db.orders": _Model("db.orders", "SELECT 1")}
        )

        result = op_render_project(str(project), {})

        assert result["rendered_project"] == RENDERED_PROJECT_DIR
        assert not result["rendered_project"].startswith("/")

    def test_a_model_name_cannot_escape_the_models_directory(
        self, project, patched_context
    ):
        """A model named with path separators must not write outside."""
        patched_context["context"] = _Context(
            {"../../etc/passwd": _Model("../../etc/passwd", "SELECT 1")}
        )

        op_render_project(str(project), {})

        models_dir = project / RENDERED_PROJECT_DIR / "models"
        written = [p.name for p in models_dir.iterdir()]
        # Separators and traversal are gone; the file is a leaf of models/.
        assert len(written) == 1
        assert "/" not in written[0] and ".." not in written[0]
        assert not (project.parent / "etc").exists()


class TestProjectSubdir:
    def test_a_subdir_outside_the_working_copy_is_refused(self, project):
        """A forged spec must not be able to point the engine elsewhere."""
        with pytest.raises(SqlMeshOpError, match="outside the working copy"):
            resolve_sqlmesh_root(str(project), "../../elsewhere")

    def test_a_missing_subdir_is_refused(self, project):
        with pytest.raises(SqlMeshOpError, match="no project directory"):
            resolve_sqlmesh_root(str(project), ".datacode/never-rendered")

    def test_an_existing_subdir_is_used(self, project):
        target = project / RENDERED_PROJECT_DIR
        target.mkdir(parents=True)

        assert resolve_sqlmesh_root(str(project), RENDERED_PROJECT_DIR) == str(
            target.resolve()
        )

    def test_no_subdir_resolves_the_default_project(self, project):
        assert resolve_sqlmesh_root(str(project)).endswith("sql-warehouse")


class TestRegistry:
    def test_render_project_is_registered_and_expects_python(self):
        handler = handlers.resolve("sqlmesh.render_project")

        # The one op whose whole purpose is projects containing Python.
        assert handler.sql_only is False
        assert handler.writes is False

    def test_apply_keeps_its_sql_only_check(self):
        """Phase 2 must not have the check disabled for the split to work."""
        assert handlers.resolve("sqlmesh.apply").sql_only is True
        assert handlers.resolve("sqlmesh.apply").writes is True

    def test_the_subdir_parameter_is_passed_through(self, project, monkeypatch):
        seen: dict = {}

        def fake_op(workspace_path, params, **kwargs):
            seen.update(kwargs)
            return {}

        monkeypatch.setitem(
            handlers._REGISTRY,
            "sqlmesh.apply",
            handlers.Handler(fake_op, sql_only=True, writes=True),
        )

        handlers.run(
            "sqlmesh.apply",
            str(project),
            {handlers.PARAM_PROJECT_SUBDIR: ".datacode/rendered"},
        )

        assert seen["project_subdir"] == ".datacode/rendered"
        assert seen["sql_only"] is True
