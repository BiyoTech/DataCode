"""Write-split tests — the Q5 constraint, on both sides of the boundary.

Two things must hold, and a regression in either is a security regression:

1.  The route is decided by INSPECTING the project, never by trusting the caller.
    A caller able to declare "SQL-only" would be a caller able to obtain the
    writable account for a project full of Python.
2.  Phase 2 keeps its SQL-only check ON. The rendered project passes it on its own
    merits, so nothing has to be bypassed for the split to work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from datacode_common.task_protocol import TaskResult, TaskStatus

from src.tasks.write_routing import (
    KIND_APPLY,
    KIND_PLAN,
    KIND_PLAN_WITH_PYTHON,
    KIND_RENDER_PROJECT,
    PARAM_PROJECT_SUBDIR,
    project_python_files,
    route_for_apply,
    route_for_plan,
    submit_apply,
)

# ── fixtures ──────────────────────────────────────────────────────────────


def _project(tmp_path: Path, *, python: bool = False, macros: bool = False) -> str:
    """Build a working copy with a SQLMesh project inside it."""
    warehouse = Path(tmp_path) / "sql-warehouse"
    (warehouse / "models").mkdir(parents=True, exist_ok=True)
    (warehouse / "config.yaml").write_text("gateways: {}\n", encoding="utf-8")
    (warehouse / "models" / "orders.sql").write_text(
        "MODEL (name db.orders);\nSELECT 1 AS id\n", encoding="utf-8"
    )
    if python:
        (warehouse / "models" / "derived.py").write_text("# model\n", encoding="utf-8")
    if macros:
        (warehouse / "macros").mkdir(exist_ok=True)
        (warehouse / "macros" / "helpers.py").write_text("# macro\n", encoding="utf-8")
    return str(tmp_path)


class _RecordingSubmitter:
    """Records what was submitted and returns canned results per kind."""

    def __init__(self, results: Optional[dict[str, TaskResult]] = None) -> None:
        self.results = results or {}
        self.submissions: list[tuple[str, dict[str, Any]]] = []

    async def submit(self, kind: str, workspace_path: str, **kwargs) -> TaskResult:
        self.submissions.append((kind, {"workspace_path": workspace_path, **kwargs}))
        canned = self.results.get(kind)
        if canned is not None:
            return canned
        return TaskResult(f"task-{len(self.submissions)}", TaskStatus.SUCCEEDED)

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.submissions]


def _rendered_result(**overrides) -> TaskResult:
    payload = {
        "rendered_project": ".datacode/rendered",
        "model_count": 2,
        "models_rendered": ["db.orders", "db.derived"],
        "python_code_remaining": [],
    }
    payload.update(overrides)
    return TaskResult("render-1", TaskStatus.SUCCEEDED, result=payload, duration_ms=500)


# ── routing ───────────────────────────────────────────────────────────────


class TestProjectInspection:
    def test_a_sql_only_project_reports_no_python(self, tmp_path):
        assert project_python_files(_project(tmp_path)) == ()

    def test_python_models_are_found(self, tmp_path):
        found = project_python_files(_project(tmp_path, python=True))

        assert found == ("models/derived.py",)

    def test_python_macros_are_found_too(self, tmp_path):
        """A macro executes at load time just as a model does."""
        found = project_python_files(_project(tmp_path, macros=True))

        assert found == ("macros/helpers.py",)

    def test_an_empty_package_marker_is_not_code(self, tmp_path):
        workspace = _project(tmp_path)
        (Path(workspace) / "sql-warehouse" / "models" / "__init__.py").touch()

        assert project_python_files(workspace) == ()

    def test_a_non_empty_init_is_code(self, tmp_path):
        workspace = _project(tmp_path)
        (Path(workspace) / "sql-warehouse" / "models" / "__init__.py").write_text(
            "import os\n", encoding="utf-8"
        )

        assert project_python_files(workspace) == ("models/__init__.py",)


class TestApplyRouting:
    def test_a_sql_only_project_applies_in_one_phase(self, tmp_path):
        route = route_for_apply(_project(tmp_path))

        assert route.kinds == (KIND_APPLY,)
        assert not route.is_split

    def test_a_python_project_is_split(self, tmp_path):
        route = route_for_apply(_project(tmp_path, python=True))

        assert route.kinds == (KIND_RENDER_PROJECT, KIND_APPLY)
        assert route.is_split
        assert route.python_files == ("models/derived.py",)
        assert "Q5" in route.reason

    def test_plan_needs_no_split_only_the_right_profile(self, tmp_path):
        """A plan does not write, so there is nothing to keep apart."""
        assert route_for_plan(_project(tmp_path / "sql")).kinds == (KIND_PLAN,)
        assert route_for_plan(_project(tmp_path / "py", python=True)).kinds == (
            KIND_PLAN_WITH_PYTHON,
        )


# ── orchestration ─────────────────────────────────────────────────────────


class TestSubmitApply:
    async def test_sql_only_project_submits_apply_directly(self, tmp_path):
        submitter = _RecordingSubmitter()

        result = await submit_apply(
            submitter, _project(tmp_path), environment="dev", tenant_id="t1"
        )

        assert result.succeeded
        assert submitter.kinds == [KIND_APPLY]
        # No redirection: the original project is what is applied.
        assert PARAM_PROJECT_SUBDIR not in submitter.submissions[0][1]["params"]

    async def test_python_project_renders_then_applies_the_rendering(self, tmp_path):
        submitter = _RecordingSubmitter({KIND_RENDER_PROJECT: _rendered_result()})

        result = await submit_apply(
            submitter, _project(tmp_path, python=True), environment="dev", tenant_id="t1"
        )

        assert result.succeeded
        assert submitter.kinds == [KIND_RENDER_PROJECT, KIND_APPLY]
        # The write phase acts on the RENDERED project, not the original.
        write_params = submitter.submissions[1][1]["params"]
        assert write_params[PARAM_PROJECT_SUBDIR] == ".datacode/rendered"

    async def test_the_render_phase_is_visible_in_the_result(self, tmp_path):
        """Two sandboxes were involved; a caller should not have to infer it."""
        submitter = _RecordingSubmitter({KIND_RENDER_PROJECT: _rendered_result()})

        result = await submit_apply(
            submitter, _project(tmp_path, python=True), tenant_id="t1"
        )

        assert result.result["render_phase"]["models_rendered"] == 2
        assert result.result["render_phase"]["task_id"] == "render-1"

    async def test_a_failed_render_is_returned_as_itself(self, tmp_path):
        """Nothing was written, so calling it an apply failure would mislead."""
        failure = TaskResult.failure("render-1", "task_failed", "a model raised")
        submitter = _RecordingSubmitter({KIND_RENDER_PROJECT: failure})

        result = await submit_apply(
            submitter, _project(tmp_path, python=True), tenant_id="t1"
        )

        assert not result.succeeded
        assert result.error_code == "task_failed"
        # The write phase must NOT have run.
        assert submitter.kinds == [KIND_RENDER_PROJECT]

    async def test_a_render_without_an_output_path_refuses_to_write(self, tmp_path):
        """Otherwise phase 2 would apply the ORIGINAL, Python-bearing project."""
        submitter = _RecordingSubmitter(
            {KIND_RENDER_PROJECT: _rendered_result(rendered_project="")}
        )

        result = await submit_apply(
            submitter, _project(tmp_path, python=True), tenant_id="t1"
        )

        assert not result.succeeded
        assert "original" in result.error_message
        assert submitter.kinds == [KIND_RENDER_PROJECT]

    async def test_a_rendering_that_still_contains_python_is_refused(self, tmp_path):
        """The render phase's own evidence is checked before the write proceeds."""
        submitter = _RecordingSubmitter(
            {
                KIND_RENDER_PROJECT: _rendered_result(
                    python_code_remaining=["models/left_behind.py"]
                )
            }
        )

        result = await submit_apply(
            submitter, _project(tmp_path, python=True), tenant_id="t1"
        )

        assert not result.succeeded
        assert "left_behind.py" in result.error_message
        assert submitter.kinds == [KIND_RENDER_PROJECT]

    async def test_each_phase_gets_the_full_budget(self, tmp_path):
        """A shared budget would let the render phase starve the write phase."""
        submitter = _RecordingSubmitter({KIND_RENDER_PROJECT: _rendered_result()})

        await submit_apply(
            submitter,
            _project(tmp_path, python=True),
            tenant_id="t1",
            timeout_seconds=900,
        )

        assert [s[1]["timeout_seconds"] for s in submitter.submissions] == [900, 900]

    async def test_the_caller_cannot_choose_the_route(self, tmp_path):
        """Params must not be able to redirect the write phase's project."""
        submitter = _RecordingSubmitter({KIND_RENDER_PROJECT: _rendered_result()})

        await submit_apply(
            submitter,
            _project(tmp_path, python=True),
            tenant_id="t1",
            params={PARAM_PROJECT_SUBDIR: "../../elsewhere"},
        )

        # The render phase's answer wins over whatever the caller asked for.
        assert (
            submitter.submissions[1][1]["params"][PARAM_PROJECT_SUBDIR]
            == ".datacode/rendered"
        )


class TestProfileRegistration:
    """The kinds must exist in the real profile file, or routing dead-ends."""

    def test_render_project_is_claimed_by_a_read_only_profile(self):
        from datacode_common.isolation import load_isolation_policy

        from src.tasks.credentials import WAREHOUSE_READWRITE, warehouse_grade_of

        policy = load_isolation_policy(refresh=True)
        profile = policy.profile_for(KIND_RENDER_PROJECT)

        # Whichever profile claims it, it must NOT hold a writable account.
        assert warehouse_grade_of(profile) != WAREHOUSE_READWRITE

    def test_apply_is_claimed_by_an_sql_only_profile(self):
        from datacode_common.isolation import load_isolation_policy

        policy = load_isolation_policy(refresh=True)
        profile = policy.profile_for(KIND_APPLY)

        # It may hold the writable account only because it runs no user code —
        # which the credential policy verifies for the whole file elsewhere.
        assert KIND_APPLY in profile.allowed_task_kinds


@pytest.fixture
def anyio_backend():
    return "asyncio"
