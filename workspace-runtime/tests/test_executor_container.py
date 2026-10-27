"""P6c vehicle tests: the container executor and the spec path rewrite.

These pin the contracts that only show up under ``executor.mode: container``
and the Kubernetes task backend — the pieces a dev machine cannot exercise
end-to-end, so their invariants are asserted directly:

* a spec crossing into a container must describe the CONTAINER's filesystem;
* the warm sandbox Pod name must be DNS-safe and deterministic;
* the server's TCP dispatch must produce the same error payloads the stdio
  form does (the runtime's error translation depends on their shape).
"""

from __future__ import annotations

import json

from datacode_common.task_protocol import TaskSpec

from src.executor.container import SANDBOX_PORT, _pod_name
from src.tasks.backends.kubernetes import WORKSPACE_MOUNT_PATH, container_spec_json


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="task0001",
        kind="sqlmesh.apply",
        workspace_path="/data/workspaces/t1/ws1/user/dev",
        environment="dev",
        tenant_id="t1",
        workspace_id="ws1",
        isolation_profile="L2_container",
        timeout_seconds=300,
    )


class TestContainerSpecRewrite:
    def test_the_path_is_rewritten_to_the_mount_point(self):
        payload = json.loads(container_spec_json(_spec()))

        assert payload["workspace_path"] == WORKSPACE_MOUNT_PATH

    def test_everything_else_is_carried_unchanged(self):
        original = _spec()
        payload = json.loads(container_spec_json(original))

        assert payload["task_id"] == original.task_id
        assert payload["kind"] == original.kind
        assert payload["environment"] == original.environment
        assert payload["tenant_id"] == original.tenant_id
        assert payload["timeout_seconds"] == original.timeout_seconds

    def test_the_rewrite_does_not_mutate_the_callers_spec(self):
        spec = _spec()
        before = spec.workspace_path

        container_spec_json(spec)

        assert spec.workspace_path == before


class TestWarmSandboxNaming:
    def test_names_are_dns_safe(self):
        name = _pod_name("/data/workspaces/Tenant_1/My Workspace/dev!", "gw")

        assert name == name.lower()
        assert all(c.isalnum() or c == "-" for c in name)
        assert name.startswith("warm-")

    def test_names_are_deterministic(self):
        assert _pod_name("/data/ws/a", "g1") == _pod_name("/data/ws/a", "g1")

    def test_different_gateways_get_different_pods(self):
        """A Context binds its connection at load time; sharing a Pod across
        gateways would serve one warehouse's questions to the other."""
        assert _pod_name("/data/ws/a", "g1") != _pod_name("/data/ws/a", "g2")

    def test_different_copies_get_different_pods(self):
        assert _pod_name("/data/ws/a", "g") != _pod_name("/data/ws/b", "g")

    def test_the_sandbox_port_is_the_documented_one(self):
        assert SANDBOX_PORT == 8010


class TestServerDispatch:
    """The TCP form shares the stdio dispatch; pinned via _dispatch directly."""

    def _dispatch(self, message: dict):
        from src.executor import server

        return server._dispatch(message)

    def test_ping(self):
        response = self._dispatch({"v": 1, "id": 1, "op": "ping", "params": {}})

        assert response["ok"] is True
        assert response["result"] == {"alive": True}

    def test_unknown_op_reports_the_op_name(self):
        response = self._dispatch({"v": 1, "id": 2, "op": "nope", "params": {}})

        assert response["ok"] is False
        assert response["error"]["code"] == "unknown_op"

    def test_the_apply_op_is_gone(self):
        """Q5: the warm sandbox must never be asked to write."""
        response = self._dispatch({"v": 1, "id": 3, "op": "sqlmesh.apply", "params": {}})

        assert response["ok"] is False
        assert response["error"]["code"] == "unknown_op"

    def test_plan_with_no_project_is_an_op_failure_not_a_crash(self):
        response = self._dispatch(
            {"v": 1, "id": 4, "op": "sqlmesh.plan", "params": {"environment": "dev"}}
        )

        assert response["ok"] is False
        assert response["error"]["code"] == "op_failed"

    def test_lint_errors_carry_their_payload(self, monkeypatch):
        """The runtime's 422 depends on lint_results riding in the error."""
        import pytest

        try:
            from sqlmesh.utils.errors import LinterError
        except ImportError:
            pytest.skip("SQLMesh LinterError not importable")

        from src.executor import server
        from src.sqlmesh import plan_engine

        monkeypatch.setattr(plan_engine, "run_tests_summary", lambda ctx: None)
        monkeypatch.setattr(
            plan_engine, "build_plan",
            lambda *a, **kw: (_ for _ in ()).throw(LinterError("bad model")),
        )
        monkeypatch.setattr(
            plan_engine, "is_linter_error", lambda exc: isinstance(exc, LinterError)
        )
        monkeypatch.setattr(
            plan_engine,
            "collect_lint_violations",
            lambda ctx: {"total": 1, "errors": 1, "report": "one error"},
        )

        # A truthy placeholder so _get_context does not try to load a project.
        monkeypatch.setitem(server._CONTEXTS, "", object())

        response = self._dispatch(
            {"v": 1, "id": 5, "op": "sqlmesh.plan", "params": {"environment": "dev"}}
        )

        assert response["ok"] is False
        assert response["error"]["code"] == "lint_failed"
        assert response["error"]["lint_results"]["total"] == 1
