"""PlanService P6c routing tests.

The service is now a thin caller of the sandbox: what these tests pin is the
ROUTING and the guards, not SQLMesh itself.

* preview must go to the warm executor and never touch a Context;
* apply must run its gate (tests / lint / no-changes) in the executor and its
  write through the task plane;
* the structured outcomes users depend on — test summaries, lint panels,
  fingerprint conflicts, no-changes — must survive the boundary crossing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import pytest

import src.sqlmesh.plan_service as plan_service_module
from src.sqlmesh.plan_service import PlanLintError, PlanService
from src.utils.errors import ConflictError, ServiceError

WS = "/data/workspaces/t1/ws1/user/env"
ENV = "dev"


class FakeExecutorManager:
    """Stands in for the warm sandbox; records calls, returns canned results."""

    def __init__(self, result: Optional[dict] = None, error: Optional[Exception] = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []
        self.invalidated: list[str] = []

    async def call(self, workspace_path, environment, op, params=None, *, gateway=""):
        self.calls.append((workspace_path, op, dict(params or {})))
        if self.error is not None:
            raise self.error
        return dict(self.result or {})

    def invalidate(self, workspace_path: str) -> None:
        self.invalidated.append(workspace_path)


class FakeSubmitResult:
    def __init__(self, succeeded: bool, result: Optional[dict] = None,
                 error_code: str = "", error_message: str = ""):
        self.succeeded = succeeded
        self.result = result or {}
        self.error_code = error_code
        self.error_message = error_message


class FakePool:
    """Stands in for the MetadataContextStore's cache-control surface."""

    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, workspace_path: str) -> None:
        self.invalidated.append(workspace_path)

    def invalidate_under_path(self, root_path: str) -> int:
        return 0


class FakeTracker:
    async def has_running_task(self, workspace_path):
        return False

    async def register(self, **kwargs):
        return True

    async def cancel(self, key):
        return False


async def _submit_apply_stub(submitter, workspace_path, **kwargs):
    """Records what the service handed to the write plane."""
    _submit_apply_stub.last_kwargs = kwargs
    _submit_apply_stub.last_workspace = workspace_path
    return _submit_apply_stub.next_result


_submit_apply_stub.last_kwargs = None
_submit_apply_stub.last_workspace = None
_submit_apply_stub.next_result = FakeSubmitResult(True, {"applied": True, "has_changes": True})


class _Env:
    """A registered-environment stand-in."""

    name = ENV
    environment_id = "env-1"


@pytest.fixture
def service(monkeypatch):
    """A PlanService with every external dependency faked."""
    svc = PlanService()

    monkeypatch.setattr(
        plan_service_module, "require_registered_environment",
        lambda ws, env: _async_value(_Env()),
    )
    monkeypatch.setattr(
        plan_service_module, "check_environment_lock",
        lambda ws, operation="": _async_value(None),
    )

    @asynccontextmanager
    async def fake_lock(ws, operation=""):
        yield

    monkeypatch.setattr(plan_service_module, "environment_lock", fake_lock)
    monkeypatch.setattr(plan_service_module, "get_task_tracker", lambda: FakeTracker())
    monkeypatch.setattr(
        svc, "_get_workspace_fingerprint", lambda ws: _async_value("abc123")
    )
    monkeypatch.setattr(svc, "_workspace_is_dirty", lambda ws: _async_value(False))
    monkeypatch.setattr(
        svc, "_save_apply_history", lambda **kw: _async_value(None)
    )
    monkeypatch.setattr(
        svc, "_publish_plan_stage", lambda *a, **kw: _async_value(None)
    )
    monkeypatch.setattr(plan_service_module, "mark_applied", lambda *a, **kw: _async_value(None))
    monkeypatch.setattr(
        plan_service_module, "_identity", lambda ws: ("t1", "ws1", "u1", ENV)
    )
    monkeypatch.setattr(plan_service_module, "_get_task_submitter", lambda: object())
    # _apply imports the metadata accessor lazily, so patching the module
    # attribute is what reaches it.
    from src.sqlmesh import metadata_context as md_module

    monkeypatch.setattr(md_module, "get_metadata_context", lambda: FakePool())
    import src.tasks.write_routing as write_routing

    monkeypatch.setattr(write_routing, "submit_apply", _submit_apply_stub)

    return svc


async def _async_value(value):
    return value


def _formatted_result(**overrides) -> dict:
    base = {
        "environment": ENV,
        "has_changes": True,
        "requires_backfill": False,
        "changes": [{"name": "db.orders"}],
        "tests_failed": False,
        "test_results": {"total": 2, "failures": 0, "errors": 0},
    }
    base.update(overrides)
    return base


class TestPreview:
    async def test_preview_runs_in_the_executor(self, service, monkeypatch):
        manager = FakeExecutorManager(result=_formatted_result())
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)

        result = await service.preview(WS, ENV)

        assert len(manager.calls) == 1
        workspace, op, params = manager.calls[0]
        assert workspace == WS
        assert op == "sqlmesh.plan"
        assert params["environment"] == ENV
        # plan_options defaults to the empty dict (the sandbox op reads
        # skip_tests etc. from it).
        assert params["plan_options"] == {}
        # The fingerprint the UI relies on is attached on this side.
        assert result["state_fingerprint"] == "abc123"

    async def test_failed_tests_return_a_structured_result(self, service, monkeypatch):
        manager = FakeExecutorManager(
            result=_formatted_result(
                tests_failed=True,
                has_changes=False,
                test_results={"total": 3, "failures": 1, "errors": 2},
            )
        )
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)

        result = await service.preview(WS, ENV)

        assert result["tests_failed"] is True
        assert result["test_results"]["failures"] == 1
        assert result["state_fingerprint"] == "abc123"

    async def test_lint_rejections_become_structured_422(self, service, monkeypatch):
        from src.executor.process import ExecutorError

        error = ExecutorError(
            "lint_failed",
            "plan blocked by linter",
            payload={
                "code": "lint_failed",
                "message": "plan blocked by linter",
                "lint_results": {"total": 4, "errors": 4, "warnings": 0},
            },
        )
        manager = FakeExecutorManager(error=error)
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)

        with pytest.raises(PlanLintError) as excinfo:
            await service.preview(WS, ENV)

        assert excinfo.value.lint_results["total"] == 4
        assert excinfo.value.status_code == 422

    async def test_executor_crash_becomes_a_service_error(self, service, monkeypatch):
        from src.executor.process import ExecutorError

        manager = FakeExecutorManager(error=ExecutorError("crashed", "executor died"))
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)

        with pytest.raises(ServiceError):
            await service.preview(WS, ENV)


class TestApplyGate:
    async def test_failed_tests_abort_before_any_submission(self, service, monkeypatch):
        manager = FakeExecutorManager(
            result=_formatted_result(
                tests_failed=True,
                test_results={"total": 1, "failures": 1, "errors": 0},
            )
        )
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)
        _submit_apply_stub.last_kwargs = None

        result = await service.apply(WS, ENV)

        assert result["applied"] is False
        assert result["reason"] == "tests_failed"
        assert result["test_results"]["failures"] == 1
        # The write plane was never reached.
        assert _submit_apply_stub.last_kwargs is None

    async def test_no_changes_short_circuits(self, service, monkeypatch):
        manager = FakeExecutorManager(
            result=_formatted_result(has_changes=False, requires_backfill=False)
        )
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)
        _submit_apply_stub.last_kwargs = None

        result = await service.apply(WS, ENV)

        assert result == {
            "applied": False,
            "reason": "no_changes",
            "has_changes": False,
            "requires_backfill": False,
            "environment": ENV,
        }
        assert _submit_apply_stub.last_kwargs is None

    async def test_a_fingerprint_conflict_is_a_409(self, service, monkeypatch):
        manager = FakeExecutorManager(result=_formatted_result())
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)

        with pytest.raises(ConflictError):
            await service.apply(WS, ENV, expected_fingerprint="different")


class TestApplyWrite:
    async def _gate_ready(self, monkeypatch, **result_overrides):
        manager = FakeExecutorManager(result=_formatted_result(**result_overrides))
        monkeypatch.setattr(plan_service_module, "get_executor_manager", lambda: manager)
        return manager

    async def test_the_write_goes_to_the_task_plane_with_the_user_options(
        self, service, monkeypatch
    ):
        await self._gate_ready(monkeypatch)
        _submit_apply_stub.next_result = FakeSubmitResult(
            True, {"applied": True, "has_changes": True, "requires_backfill": False}
        )
        _submit_apply_stub.last_kwargs = None

        result = await service.apply(
            WS, ENV,
            plan_options={"forward_only": True},
            categories={"db.orders": "NON_BREAKING"},
            user_id="user-7",
        )

        assert result["applied"] is True
        assert result["applied_commit"] == "abc123"
        kwargs = _submit_apply_stub.last_kwargs
        assert _submit_apply_stub.last_workspace == WS
        assert kwargs["environment"] == ENV
        assert kwargs["tenant_id"] == "t1"
        assert kwargs["actor_user_id"] == "user-7"
        assert kwargs["params"]["categories"] == {"db.orders": "NON_BREAKING"}
        assert kwargs["params"]["plan_options"] == {"forward_only": True}

    async def test_a_failed_task_maps_to_apply_failed(self, service, monkeypatch):
        await self._gate_ready(monkeypatch)
        _submit_apply_stub.next_result = FakeSubmitResult(
            False, error_code="task_failed", error_message="boom"
        )

        with pytest.raises(ServiceError) as excinfo:
            await service.apply(WS, ENV)

        assert "boom" in str(excinfo.value)

    async def test_success_invalidates_both_context_caches(
        self, service, monkeypatch
    ):
        """The warehouse changed; a stale view is a stale answer."""
        manager = await self._gate_ready(monkeypatch)
        store = FakePool()
        from src.sqlmesh import metadata_context as md_module

        monkeypatch.setattr(md_module, "get_metadata_context", lambda: store)
        _submit_apply_stub.next_result = FakeSubmitResult(
            True, {"applied": True, "has_changes": True}
        )

        await service.apply(WS, ENV)

        assert manager.invalidated == [WS]
        assert store.invalidated == [WS]

    async def test_a_task_side_lint_failure_is_still_a_422(self, service, monkeypatch):
        """Lint that slipped past the gate (files changed mid-flight)."""
        await self._gate_ready(monkeypatch)
        _submit_apply_stub.next_result = FakeSubmitResult(
            False, error_code="lint_failed", error_message="Linter errors for x.sql: ..."
        )

        with pytest.raises(PlanLintError):
            await service.apply(WS, ENV)
