"""The shell endpoint's dispatch: which plane actually receives a command.

Asserted through the REAL FastAPI route, because the routing decision is the
security property and it is made inside that handler. Unit-testing ``classify``
alone would not catch a handler that classifies correctly and then submits to the
sandbox anyway.

Two stubs, at the narrowest possible seams:

* the task submitter — a real sandbox would need a task-runner process, and what
  is under test is WHETHER it is called, not what it does;
* the governance services (``PlanService`` and friends) — they need a SQLMesh
  project and a warehouse; again, the assertion is about reaching them.

Everything between the HTTP request and those two seams is production code.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RecordingSubmitter:
    """Stands in for the sandbox plane, recording what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit(self, kind, workspace_path, **kwargs):
        from datacode_common.task_protocol import TaskResult, TaskStatus

        self.calls.append(
            {"kind": kind, "workspace_path": workspace_path, **kwargs}
        )
        return TaskResult(
            task_id="t-1",
            status=TaskStatus.SUCCEEDED,
            result={
                "exit_code": 0,
                "stdout": "SANDBOX",
                "stderr": "",
                "timed_out": False,
            },
        )


class _RecordingPlanService:
    """Stands in for the governance plane."""

    def __init__(self) -> None:
        self.previews: list[dict] = []
        self.applies: list[dict] = []

    async def preview(self, **kwargs):
        self.previews.append(kwargs)
        return {"has_changes": False}

    async def apply(self, **kwargs):
        self.applies.append(kwargs)
        return {
            "applied": True,
            "changes": {"added": [], "removed": [], "modified": {}},
            "backfills": [],
        }


@pytest.fixture()
def planes(monkeypatch):
    """Wire both planes to recorders and return them."""
    from src.api.rest import tools as tools_api

    # Path validation and identity extraction are covered by their own tests;
    # here they would only require a materialized working copy on disk.
    monkeypatch.setattr(tools_api, "validate_workspace_path", lambda p: p)
    monkeypatch.setattr(
        tools_api, "_identity", lambda p: ("t1", "ws-1", "u-owner", "dev")
    )

    submitter = _RecordingSubmitter()
    plan_service = _RecordingPlanService()
    monkeypatch.setattr(
        "src.tasks.submitter.get_task_submitter", lambda: submitter
    )
    monkeypatch.setattr("src.api.deps.get_plan_service", lambda: plan_service)

    app = FastAPI()
    app.include_router(tools_api.router)
    return {
        "client": TestClient(app),
        "submitter": submitter,
        "plan_service": plan_service,
    }


def _shell(planes, command: str, **extra):
    return planes["client"].post(
        "/rt/v1/tools/shell",
        json={
            "workspace_path": "/w/copy",
            "command": command,
            "timeout_s": 5,
            **extra,
        },
    )


class TestSandboxPath:
    @pytest.mark.parametrize(
        "command", ["ls -la", "pytest -q", "sqlmesh test", "git status"]
    )
    def test_an_ordinary_command_goes_to_the_sandbox(self, planes, command):
        response = _shell(planes, command)

        assert response.status_code == 200
        assert response.json()["stdout"] == "SANDBOX"
        assert len(planes["submitter"].calls) == 1
        assert planes["submitter"].calls[0]["kind"] == "agent.shell"
        assert planes["plan_service"].previews == []

    def test_a_compound_command_containing_a_governed_verb_goes_to_the_sandbox(
        self, planes
    ):
        """It cannot be parsed into argv without a shell, so it is not routed.

        The sandbox's generated guards refuse the governed verb there — this test
        asserts the DISPATCH, the guard behaviour is asserted in
        shared/python/tests/test_command_verbs.py.
        """
        response = _shell(planes, "sqlmesh apply prod && echo done")

        assert response.status_code == 200
        assert len(planes["submitter"].calls) == 1
        assert planes["plan_service"].applies == []


class TestGovernancePath:
    def test_plan_reaches_the_plan_service_and_not_the_sandbox(self, planes):
        response = _shell(planes, "sqlmesh plan dev")

        assert response.status_code == 200
        assert response.json()["stdout"] == "No changes to apply."
        assert planes["submitter"].calls == []
        assert len(planes["plan_service"].previews) == 1

    def test_the_environment_comes_from_the_working_copy(self, planes):
        """A named environment cannot redirect the operation: the working copy is
        per-environment, and honouring the argument would let a hallucinated
        token deploy elsewhere."""
        response = _shell(planes, "sqlmesh apply prod")

        assert response.status_code == 200
        call = planes["plan_service"].applies[0]
        assert call["environment"] == "dev"
        # And the disagreement is reported rather than swallowed.
        assert "prod" in response.json()["stderr"]

    def test_apply_is_always_auto_applied(self, planes):
        """`apply` IS an auto-applied plan; the caller does not get to choose."""
        _shell(planes, "sqlmesh apply dev")

        assert planes["plan_service"].applies[0]["plan_options"]["auto_apply"] is True

    def test_plan_auto_apply_is_refused(self, planes):
        """Preview and deploy are separate commands, and the shim refused this
        too — a plan that deploys defeats the approval step."""
        response = _shell(planes, "sqlmesh plan dev --auto-apply")

        assert response.status_code == 200
        body = response.json()
        assert body["exit_code"] != 0
        assert "disabled on this platform" in body["stderr"]
        assert planes["plan_service"].previews == []
        assert planes["submitter"].calls == []

    def test_attribution_is_taken_from_the_request(self, planes):
        """The session is known on the agent side only, which is why routing had
        to move to this side: the sandbox was never told and could not be."""
        _shell(planes, "sqlmesh apply dev", user_id="u-actor", session_id="s-9")

        call = planes["plan_service"].applies[0]
        assert call["user_id"] == "u-actor"
        assert call["session_id"] == "s-9"
        assert call["trigger_type"] == "agent"

    def test_attribution_falls_back_to_the_copys_owner(self, planes):
        _shell(planes, "sqlmesh apply dev")

        assert planes["plan_service"].applies[0]["user_id"] == "u-owner"

    def test_sqlmesh_run_is_refused_without_touching_either_plane(self, planes):
        response = _shell(planes, "sqlmesh run")

        body = response.json()
        assert body["exit_code"] != 0
        assert "disabled on this platform" in body["stderr"]
        assert planes["submitter"].calls == []
        assert planes["plan_service"].applies == []

    def test_a_usage_error_is_a_failed_command_not_an_http_error(self, planes):
        """The caller asked to run a command; a command that failed to parse is a
        tool result. A 4xx would read as "the platform is broken"."""
        response = _shell(planes, "sqlmesh plan dev --no-such-flag")

        assert response.status_code == 200
        body = response.json()
        assert body["exit_code"] != 0
        assert "no-such-flag" in body["stderr"]

    def test_a_domain_failure_is_rendered_with_its_error_code(self, planes):
        """A state-schema mismatch, a lost optimistic lock, a busy environment:
        the agent can act on the code, so it must survive the rendering."""
        from src.utils.errors import ErrorCode, ServiceError

        async def _boom(**_kwargs):
            raise ServiceError(
                ErrorCode.ENVIRONMENT_BUSY,
                "another apply is running",
                status_code=409,
            )

        planes["plan_service"].apply = _boom
        response = _shell(planes, "sqlmesh apply dev")

        assert response.status_code == 200
        body = response.json()
        assert body["exit_code"] != 0
        assert "ENVIRONMENT_BUSY" in body["stderr"]
        assert "another apply is running" in body["stderr"]


class TestResultShapeIsIndistinguishable:
    """agent-service reads one shape. If the two planes disagreed on it, the
    tool schema would have to grow a discriminator — and the LLM would start
    reasoning about which plane ran its command."""

    def test_both_planes_return_the_same_keys(self, planes):
        sandbox = _shell(planes, "ls").json()
        governed = _shell(planes, "sqlmesh plan dev").json()

        assert set(sandbox) == set(governed) == {
            "exit_code",
            "stdout",
            "stderr",
            "timed_out",
        }
