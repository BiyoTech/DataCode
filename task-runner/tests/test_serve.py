"""Resident runner tests — the warm-pool server side.

Covers the three properties that make it safe to keep a task vehicle alive, and
they are exactly the ones that do NOT exist in the one-shot form:

* credentials belong to a TASK, not to the Pod: applied for one task, removed
  afterwards, including when the task fails;
* the workspace is fixed by the mount and re-checked per request, so a
  mis-routed task is refused instead of run against the wrong project;
* one task at a time — a second concurrent task is answered ``busy`` rather than
  queued, because the pool provides parallelism by holding several Pods.

The handler is driven directly (no socket) except in the framing test: what
matters here is the decision table, and a real socket is exercised end to end by
the smoke path in the runtime suite.
"""

from __future__ import annotations

import io
import os

import pytest
from datacode_common.framing import encode, read_frame_sync
from datacode_common.task_protocol import TaskResult, TaskSpec, TaskStatus
from datacode_common.warm_protocol import (
    ERR_BAD_REQUEST,
    ERR_BUSY,
    ERR_UNKNOWN_OP,
    ERR_WRONG_WORKSPACE,
    OP_PING,
    OP_RUN,
    OP_SHUTDOWN,
    make_run_request,
)

from src.serve import ResidentRunner, _apply_credentials

WS = "/workspace"


def _spec(task_id: str = "t1", kind: str = "agent.shell", workspace: str = WS) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind=kind,
        workspace_path=workspace,
        environment="dev",
        tenant_id="t1",
        workspace_id="ws-t1-abc",
        isolation_profile="L3_base",
        params={"command": "true"},
        timeout_seconds=60,
    )


@pytest.fixture
def runner(monkeypatch):
    """A resident runner whose task execution is stubbed and observable."""
    resident = ResidentRunner(WS)
    seen: list[tuple[str, dict]] = []

    def fake_run_task(spec: TaskSpec) -> TaskResult:
        # Snapshot the environment AS THE TASK SEES IT: that is the only place
        # where "credentials were applied" is observable.
        seen.append(
            (
                spec.task_id,
                {
                    key: os.environ.get(key, "")
                    for key in ("DATACODE_TASK_TOKEN", "DATACODE_EGRESS_PROXY")
                },
            )
        )
        return TaskResult(
            task_id=spec.task_id, status=TaskStatus.SUCCEEDED, result={"ok": True}
        )

    import src.main as main_module

    monkeypatch.setattr(main_module, "run_task", fake_run_task)
    return resident, seen


class TestDispatch:
    def test_ping_reports_liveness_and_busy_state(self, runner):
        resident, _ = runner

        response = resident.handle({"v": 1, "id": 1, "op": OP_PING})

        assert response["ok"] is True
        assert response["result"] == {"alive": True, "busy": False}

    def test_an_unknown_op_is_an_error_not_a_crash(self, runner):
        resident, _ = runner

        response = resident.handle({"v": 1, "id": 1, "op": "nonsense"})

        assert response["ok"] is False
        assert response["error"]["code"] == ERR_UNKNOWN_OP

    def test_shutdown_is_acknowledged_and_stops_the_loop(self, runner):
        resident, _ = runner

        response = resident.handle({"v": 1, "id": 1, "op": OP_SHUTDOWN})

        assert response["result"] == {"stopping": True}
        assert resident.stopping is True

    def test_a_run_request_executes_the_task(self, runner):
        resident, seen = runner

        response = resident.handle(make_run_request(1, _spec()))

        assert response["ok"] is True
        assert response["result"]["status"] == "succeeded"
        assert [task_id for task_id, _ in seen] == ["t1"]

    def test_a_malformed_request_is_refused(self, runner):
        resident, _ = runner

        response = resident.handle({"v": 1, "id": 1, "op": OP_RUN})

        assert response["error"]["code"] == ERR_BAD_REQUEST

    def test_a_wrong_protocol_version_is_refused(self, runner):
        """A silently ignored field could be an isolation constraint."""
        resident, _ = runner
        request = make_run_request(1, _spec())
        request["v"] = 99

        response = resident.handle(request)

        assert response["error"]["code"] == ERR_BAD_REQUEST


class TestWorkspaceGuard:
    def test_a_request_for_another_copy_is_refused(self, runner):
        """The mount already makes other copies unreachable; refusing here turns
        a routing bug into a clear error instead of 'project not found'."""
        resident, seen = runner

        response = resident.handle(
            make_run_request(1, _spec(workspace="/workspace-other"))
        )

        assert response["ok"] is False
        assert response["error"]["code"] == ERR_WRONG_WORKSPACE
        assert seen == []  # nothing ran

    def test_the_served_path_is_reported_in_the_error(self, runner):
        resident, _ = runner

        response = resident.handle(
            make_run_request(1, _spec(workspace="/elsewhere"))
        )

        assert WS in response["error"]["message"]
        assert "/elsewhere" in response["error"]["message"]


class TestPerTaskCredentials:
    def test_credentials_are_visible_to_the_task(self, runner):
        resident, seen = runner

        resident.handle(
            make_run_request(
                1,
                _spec(),
                {"DATACODE_TASK_TOKEN": "tok-1", "DATACODE_EGRESS_PROXY": "http://p:3128"},
            )
        )

        _, env = seen[0]
        assert env["DATACODE_TASK_TOKEN"] == "tok-1"
        assert env["DATACODE_EGRESS_PROXY"] == "http://p:3128"

    def test_credentials_do_not_outlive_the_task(self):
        """THE property that makes a resident Pod acceptable: after a task, the
        Pod holds no warehouse credential at all."""
        assert os.environ.get("DATACODE_TASK_TOKEN") in (None, "")

    def test_the_next_task_does_not_inherit_the_previous_token(self, runner):
        resident, seen = runner

        resident.handle(
            make_run_request(1, _spec("t1"), {"DATACODE_TASK_TOKEN": "tok-1"})
        )
        resident.handle(make_run_request(2, _spec("t2")))

        assert seen[0][1]["DATACODE_TASK_TOKEN"] == "tok-1"
        assert seen[1][1]["DATACODE_TASK_TOKEN"] == ""

    def test_a_failing_task_still_clears_its_credentials(self, monkeypatch):
        """The failure path is where a leaked token would hide."""
        resident = ResidentRunner(WS)

        def boom(spec):
            raise RuntimeError("handler exploded")

        import src.main as main_module

        monkeypatch.setattr(main_module, "run_task", boom)

        with pytest.raises(RuntimeError):
            resident.handle(
                make_run_request(1, _spec(), {"DATACODE_TASK_TOKEN": "tok-leak"})
            )

        assert os.environ.get("DATACODE_TASK_TOKEN") in (None, "")

    def test_a_pre_existing_value_is_restored_not_deleted(self, runner, monkeypatch):
        """A variable the vehicle itself set must survive a task that overrode it."""
        resident, _ = runner
        monkeypatch.setenv("DATACODE_EGRESS_PROXY", "http://vehicle:3128")

        resident.handle(
            make_run_request(1, _spec(), {"DATACODE_EGRESS_PROXY": "http://task:3128"})
        )

        assert os.environ["DATACODE_EGRESS_PROXY"] == "http://vehicle:3128"


class TestApplyCredentials:
    def test_it_reports_what_is_needed_to_undo(self):
        os.environ.pop("VERIFY_WARM_A", None)
        os.environ["VERIFY_WARM_B"] = "before"

        undo = _apply_credentials({"VERIFY_WARM_A": "x", "VERIFY_WARM_B": "after"})

        assert os.environ["VERIFY_WARM_A"] == "x"
        assert os.environ["VERIFY_WARM_B"] == "after"
        assert undo == {"VERIFY_WARM_A": "", "VERIFY_WARM_B": "before"}

        _apply_credentials(undo, replace=True)
        assert "VERIFY_WARM_A" not in os.environ
        assert os.environ["VERIFY_WARM_B"] == "before"
        os.environ.pop("VERIFY_WARM_B", None)


class TestBusy:
    def test_a_second_concurrent_task_is_told_to_go_elsewhere(self, monkeypatch):
        """Not queued: two tasks in one Pod share its memory limit, CPU quota
        and SQLMesh cache. The pool retries on another Pod."""
        resident = ResidentRunner(WS)
        responses: list[dict] = []

        def reentrant(spec):
            # Runs while the first task holds the runner.
            responses.append(resident.handle(make_run_request(2, _spec("t2"))))
            return TaskResult(
                task_id=spec.task_id, status=TaskStatus.SUCCEEDED, result={}
            )

        import src.main as main_module

        monkeypatch.setattr(main_module, "run_task", reentrant)

        first = resident.handle(make_run_request(1, _spec("t1")))

        assert first["ok"] is True
        assert responses[0]["error"]["code"] == ERR_BUSY

    def test_busy_clears_after_the_task(self, runner):
        resident, _ = runner

        resident.handle(make_run_request(1, _spec()))
        ping = resident.handle({"v": 1, "id": 2, "op": OP_PING})

        assert ping["result"]["busy"] is False


class TestServiceLife:
    def test_tasks_served_is_counted(self, runner):
        """The pool retires a runner by this count: a task can damage its own
        interpreter and the next one would inherit it."""
        resident, _ = runner

        resident.handle(make_run_request(1, _spec("t1")))
        resident.handle(make_run_request(2, _spec("t2")))

        assert resident.tasks_served == 2

    def test_a_refused_request_does_not_count_as_served(self, runner):
        resident, _ = runner

        resident.handle(make_run_request(1, _spec(workspace="/elsewhere")))

        assert resident.tasks_served == 0


class TestFraming:
    def test_a_response_round_trips_through_the_shared_codec(self, runner):
        """The wire format is the shared one, so a runtime built against
        datacode_common.framing can read it without a private decoder."""
        resident, _ = runner
        response = resident.handle(make_run_request(1, _spec()))

        stream = io.BytesIO(encode(response))
        decoded = read_frame_sync(stream)

        assert decoded == response
