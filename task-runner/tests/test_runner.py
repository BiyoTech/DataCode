"""Tests for the task-runner entry point and dispatch (design 04 §3, P6a).

The emphasis is on the two behaviours the submitter depends on: a result line is
ALWAYS produced (so "no result" unambiguously means the vehicle died), and an
SQL-only profile refuses a project containing host-language code (the Q5
constraint that keeps the writable warehouse account away from user code).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from datacode_common.task_protocol import (
    ERR_BAD_SPEC,
    ERR_INTERNAL,
    ERR_SANDBOX_OOM,
    ERR_UNSUPPORTED_KIND,
    STREAM_PREFIX,
    TaskResult,
    TaskSpec,
    TaskStatus,
    extract_result,
)

from src import handlers, sqlmesh_ops
from src import main as runner_main
from src.sqlmesh_ops import SqlMeshOpError


@pytest.fixture
def sql_project(tmp_path: Path) -> Path:
    """A working copy whose SQLMesh project contains only SQL."""
    warehouse = tmp_path / sqlmesh_ops.SQL_WAREHOUSE_DIR
    (warehouse / "models").mkdir(parents=True)
    (warehouse / "config.yaml").write_text("gateways: {}\n")
    (warehouse / "models" / "orders.sql").write_text("SELECT 1 AS id")
    return tmp_path


@pytest.fixture
def python_project(sql_project: Path) -> Path:
    """The same working copy, plus a Python model."""
    models = sql_project / sqlmesh_ops.SQL_WAREHOUSE_DIR / "models"
    (models / "enriched.py").write_text("def execute(context):\n    return None\n")
    return sql_project


def _spec(workspace_path: Path, **overrides) -> TaskSpec:
    base = {
        "task_id": "task-test-1",
        "kind": "sqlmesh.plan",
        "workspace_path": str(workspace_path),
        "environment": "dev",
        "tenant_id": "tenant-1",
        "isolation_profile": "L2_container",
    }
    base.update(overrides)
    return TaskSpec(**base)


def _write_spec(tmp_path: Path, spec: TaskSpec) -> str:
    path = tmp_path / "spec.json"
    path.write_text(spec.to_json(), encoding="utf-8")
    return str(path)


class TestProjectResolution:
    def test_resolves_the_sql_warehouse_subdirectory(self, sql_project: Path):
        resolved = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        assert resolved == str(sql_project / sqlmesh_ops.SQL_WAREHOUSE_DIR)

    def test_accepts_a_path_that_is_itself_a_project(self, tmp_path: Path):
        """The warm sandbox (P6c) may mount the project directly."""
        (tmp_path / "config.yaml").write_text("gateways: {}\n")

        assert sqlmesh_ops.resolve_sqlmesh_root(str(tmp_path)) == str(tmp_path)

    def test_rejects_a_path_that_is_neither(self, tmp_path: Path):
        """Guessing here would report 'no models' instead of a config error."""
        with pytest.raises(SqlMeshOpError, match="no SQLMesh project"):
            sqlmesh_ops.resolve_sqlmesh_root(str(tmp_path))


class TestSqlOnlyPrecondition:
    """L2 holds the writable account; user code must not run under it."""

    def test_a_sql_only_project_passes(self, sql_project: Path):
        root = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        sqlmesh_ops.assert_sql_only(root)  # must not raise

    def test_a_python_model_is_refused(self, python_project: Path):
        root = sqlmesh_ops.resolve_sqlmesh_root(str(python_project))

        with pytest.raises(SqlMeshOpError, match="host-language code"):
            sqlmesh_ops.assert_sql_only(root)

    def test_a_python_macro_is_refused(self, sql_project: Path):
        macros = sql_project / sqlmesh_ops.SQL_WAREHOUSE_DIR / "macros"
        macros.mkdir()
        (macros / "helpers.py").write_text("def helper():\n    return 1\n")
        root = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        with pytest.raises(SqlMeshOpError, match="host-language code"):
            sqlmesh_ops.assert_sql_only(root)

    def test_the_offending_file_is_named(self, python_project: Path):
        """An operator refused an L2 run needs to know which file caused it."""
        root = sqlmesh_ops.resolve_sqlmesh_root(str(python_project))

        with pytest.raises(SqlMeshOpError, match="enriched.py"):
            sqlmesh_ops.assert_sql_only(root)

    def test_empty_package_markers_are_ignored(self, sql_project: Path):
        models = sql_project / sqlmesh_ops.SQL_WAREHOUSE_DIR / "models"
        (models / "__init__.py").write_text("")
        root = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        sqlmesh_ops.assert_sql_only(root)  # must not raise

    def test_a_non_empty_init_is_not_ignored(self, sql_project: Path):
        models = sql_project / sqlmesh_ops.SQL_WAREHOUSE_DIR / "models"
        (models / "__init__.py").write_text("import os\n")
        root = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        with pytest.raises(SqlMeshOpError, match="host-language code"):
            sqlmesh_ops.assert_sql_only(root)

    def test_sql_files_are_not_code(self, sql_project: Path):
        root = sqlmesh_ops.resolve_sqlmesh_root(str(sql_project))

        assert sqlmesh_ops.find_python_code(root) == []


class TestDispatch:
    def test_the_registry_is_explicit(self):
        kinds = handlers.supported_kinds()

        assert "sqlmesh.plan" in kinds
        assert "sqlmesh.apply" in kinds
        assert "sqlmesh.query" in kinds

    def test_write_operations_are_flagged(self):
        """The audit trail must tell a failed read from a partial write."""
        assert handlers.resolve("sqlmesh.apply").writes
        assert not handlers.resolve("sqlmesh.plan").writes

    def test_l2_kinds_enforce_sql_only(self):
        assert handlers.resolve("sqlmesh.plan").sql_only
        assert handlers.resolve("sqlmesh.apply").sql_only
        assert handlers.resolve("sqlmesh.query").sql_only

    def test_the_python_kind_permits_user_code(self):
        """It runs in L3-base with a read-only account, so code is expected."""
        assert not handlers.resolve("sqlmesh.plan_with_python").sql_only

    def test_an_unknown_kind_is_refused_with_a_distinct_code(self):
        with pytest.raises(SqlMeshOpError) as excinfo:
            handlers.resolve("sqlmesh.nonsense")

        assert excinfo.value.code == ERR_UNSUPPORTED_KIND


class TestRunTask:
    """run_task never raises: every outcome is a result."""

    def test_a_refused_sql_only_project_yields_a_bad_spec_result(
        self, python_project: Path
    ):
        result = runner_main.run_task(_spec(python_project))

        assert result.status is TaskStatus.FAILED
        assert result.error_code == ERR_BAD_SPEC
        assert "host-language code" in result.error_message

    def test_an_unsupported_kind_yields_a_result_not_a_crash(self, sql_project: Path):
        result = runner_main.run_task(_spec(sql_project, kind="nope.nothing"))

        assert result.error_code == ERR_UNSUPPORTED_KIND

    def test_a_handler_crash_becomes_an_internal_error_result(
        self, sql_project: Path, monkeypatch
    ):
        def _explode(*_args, **_kwargs):
            raise RuntimeError("engine imploded")

        monkeypatch.setattr(handlers, "run", _explode)

        result = runner_main.run_task(_spec(sql_project))

        assert result.error_code == ERR_INTERNAL
        assert "engine imploded" in result.error_message

    def test_a_timeout_is_reported_as_timed_out(self, sql_project: Path, monkeypatch):
        def _too_slow(*_args, **_kwargs):
            raise TimeoutError("task exceeded its 1s budget")

        monkeypatch.setattr(handlers, "run", _too_slow)

        result = runner_main.run_task(_spec(sql_project, timeout_seconds=1))

        assert result.status is TaskStatus.TIMED_OUT
        assert result.exit_code == runner_main.EXIT_TIMEOUT

    def test_memory_exhaustion_is_reported(self, sql_project: Path, monkeypatch):
        """Memory exhaustion is the SANDBOX_OOM contract verdict, not a
        generic internal error: the user's remedy is raising the limit, not
        fixing code (design 08 §7)."""
        def _oom(*_args, **_kwargs):
            raise MemoryError()

        monkeypatch.setattr(handlers, "run", _oom)

        result = runner_main.run_task(_spec(sql_project))

        assert result.error_code == ERR_SANDBOX_OOM
        assert "memory limit" in result.error_message

    def test_a_successful_run_carries_the_handler_payload(
        self, sql_project: Path, monkeypatch
    ):
        monkeypatch.setattr(
            handlers, "run", lambda *_a, **_k: {"environment": "dev", "direct": ["m1"]}
        )

        result = runner_main.run_task(_spec(sql_project))

        assert result.succeeded
        assert result.result["direct"] == ["m1"]
        assert result.duration_ms >= 0

    def test_progress_is_forwarded_to_stdout(
        self, sql_project: Path, monkeypatch, capsys
    ):
        def _with_progress(*_args, progress=None, **_kwargs):
            progress("planning", 50, "2 models")
            return {}

        monkeypatch.setattr(handlers, "run", _with_progress)

        runner_main.run_task(_spec(sql_project))

        captured = capsys.readouterr().out
        assert STREAM_PREFIX in captured
        assert "planning" in captured


class TestMain:
    """The process contract: one result line, and a matching exit code."""

    def test_a_valid_spec_runs_and_reports(
        self, sql_project: Path, tmp_path: Path, monkeypatch, capsys
    ):
        monkeypatch.setattr(handlers, "run", lambda *_a, **_k: {"ok": True})
        spec_path = _write_spec(tmp_path, _spec(sql_project))

        exit_code = runner_main.main([spec_path])

        assert exit_code == runner_main.EXIT_OK
        result = extract_result(capsys.readouterr().out.splitlines())
        assert result is not None and result.succeeded

    def test_a_missing_spec_still_produces_a_result(self, monkeypatch, capsys):
        monkeypatch.delenv(runner_main.SPEC_PATH_ENV, raising=False)

        exit_code = runner_main.main([])

        assert exit_code == runner_main.EXIT_BAD_SPEC
        result = extract_result(capsys.readouterr().out.splitlines())
        assert result is not None
        assert result.error_code == ERR_BAD_SPEC

    def test_an_unreadable_spec_path_produces_a_result(self, capsys):
        exit_code = runner_main.main(["/nonexistent/spec.json"])

        assert exit_code == runner_main.EXIT_BAD_SPEC
        result = extract_result(capsys.readouterr().out.splitlines())
        assert result is not None and "cannot read task spec" in result.error_message

    def test_a_malformed_spec_produces_a_result(self, tmp_path: Path, capsys):
        bad = tmp_path / "spec.json"
        bad.write_text("{not json", encoding="utf-8")

        exit_code = runner_main.main([str(bad)])

        assert exit_code == runner_main.EXIT_BAD_SPEC
        assert extract_result(capsys.readouterr().out.splitlines()) is not None

    def test_a_spec_carrying_a_credential_is_refused(
        self, sql_project: Path, tmp_path: Path, capsys
    ):
        """Defence in depth: the runner re-validates what the submitter built."""
        payload = _spec(sql_project).to_dict()
        payload["params"] = {"db_password": "hunter2"}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        exit_code = runner_main.main([str(path)])

        assert exit_code == runner_main.EXIT_BAD_SPEC
        result = extract_result(capsys.readouterr().out.splitlines())
        assert result is not None and "credential" in result.error_message

    def test_the_spec_path_can_come_from_the_environment(
        self, sql_project: Path, tmp_path: Path, monkeypatch, capsys
    ):
        monkeypatch.setattr(handlers, "run", lambda *_a, **_k: {})
        monkeypatch.setenv(
            runner_main.SPEC_PATH_ENV, _write_spec(tmp_path, _spec(sql_project))
        )

        assert runner_main.main([]) == runner_main.EXIT_OK
        assert extract_result(capsys.readouterr().out.splitlines()) is not None

    def test_failure_exit_code_matches_the_result_status(
        self, sql_project: Path, tmp_path: Path, monkeypatch, capsys
    ):
        def _fail(*_a, **_k):
            raise SqlMeshOpError("task_failed", "plan failed")

        monkeypatch.setattr(handlers, "run", _fail)
        spec_path = _write_spec(tmp_path, _spec(sql_project))

        exit_code = runner_main.main([spec_path])

        assert exit_code == runner_main.EXIT_TASK_FAILED
        result = extract_result(capsys.readouterr().out.splitlines())
        assert result is not None and result.status is TaskStatus.FAILED

    def test_diagnostics_go_to_stderr_not_stdout(
        self, sql_project: Path, tmp_path: Path, monkeypatch, capsys
    ):
        """stdout is the result channel; mixing them could corrupt a result."""
        monkeypatch.setattr(handlers, "run", lambda *_a, **_k: {})
        spec_path = _write_spec(tmp_path, _spec(sql_project))

        runner_main.main([spec_path])

        captured = capsys.readouterr()
        assert "task task-test-1" in captured.err
        assert all(
            line.startswith(STREAM_PREFIX)
            for line in captured.out.splitlines()
            if line.strip()
        )


class TestResultShape:
    def test_the_result_is_parsable_by_the_shared_protocol(
        self, sql_project: Path, tmp_path: Path, monkeypatch, capsys
    ):
        """Runtime and runner must agree without a second parser."""
        monkeypatch.setattr(
            handlers, "run", lambda *_a, **_k: {"environment": "dev", "plan_id": "p1"}
        )
        spec_path = _write_spec(tmp_path, _spec(sql_project))

        runner_main.main([spec_path])

        result = extract_result(capsys.readouterr().out.splitlines())
        assert isinstance(result, TaskResult)
        assert result.task_id == "task-test-1"
        assert result.result["plan_id"] == "p1"
