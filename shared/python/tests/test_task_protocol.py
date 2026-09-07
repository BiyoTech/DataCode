"""Tests for the task-runner wire protocol (design 04 §3, P6a)."""

from __future__ import annotations

import pytest

from datacode_common.task_protocol import (
    MAX_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    STREAM_PREFIX,
    TaskProgress,
    TaskProtocolError,
    TaskResult,
    TaskSpec,
    TaskStatus,
    encode_stream_line,
    extract_result,
    parse_stream_line,
)


def _spec(**overrides) -> TaskSpec:
    base = {
        "task_id": "task-abc123",
        "kind": "sqlmesh.plan",
        "workspace_path": "/workspace",
        "environment": "dev",
        "tenant_id": "tenant-1",
        "workspace_id": "tenant-1__analytics",
        "isolation_profile": "L2_container",
    }
    base.update(overrides)
    return TaskSpec(**base)


class TestSpecValidation:
    def test_a_well_formed_spec_validates(self):
        assert _spec().validate().kind == "sqlmesh.plan"

    @pytest.mark.parametrize(
        "task_id",
        [
            "Task-ABC",       # uppercase
            "task_abc",       # underscore
            "task.abc",       # dot
            "-leading",
            "trailing-",
            "",
            "a" * 64,         # too long for a DNS-1123 label
        ],
    )
    def test_ids_unusable_as_kubernetes_names_are_rejected(self, task_id):
        with pytest.raises(TaskProtocolError, match="DNS-1123"):
            _spec(task_id=task_id).validate()

    @pytest.mark.parametrize(
        "kind", ["sqlmesh", "SQLMesh.plan", "sqlmesh-plan", "sqlmesh.plan.extra", ""]
    )
    def test_malformed_kinds_are_rejected(self, kind):
        with pytest.raises(TaskProtocolError, match="domain.action"):
            _spec(kind=kind).validate()

    def test_workspace_path_is_required(self):
        with pytest.raises(TaskProtocolError, match="workspace_path is required"):
            _spec(workspace_path="").validate()

    @pytest.mark.parametrize("timeout", [0, -1, MAX_TIMEOUT_SECONDS + 1])
    def test_timeouts_outside_the_bound_are_rejected(self, timeout):
        with pytest.raises(TaskProtocolError, match="timeout_seconds"):
            _spec(timeout_seconds=timeout).validate()


class TestCredentialRejection:
    """A spec becomes a ConfigMap and an audit record; it is not a secret."""

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "db_password",
            "PASSWD",
            "client_secret",
            "task_token",
            "credential",
            "private_key",
            "aws_access_key",
            "api_key",
            "apiKey",
        ],
    )
    def test_credential_looking_keys_are_rejected(self, key):
        with pytest.raises(TaskProtocolError, match="looks like a credential"):
            _spec(params={key: "whatever"}).validate()

    def test_nested_credentials_are_rejected(self):
        with pytest.raises(TaskProtocolError, match=r"params\.connection\.password"):
            _spec(params={"connection": {"host": "db", "password": "hunter2"}}).validate()

    def test_values_mentioning_secrets_are_allowed(self):
        """Matching values would reject legitimate SQL and prose."""
        spec = _spec(
            params={"sql": "SELECT * FROM users WHERE password_reset IS NOT NULL"}
        ).validate()

        assert "password_reset" in spec.params["sql"]

    def test_ordinary_params_pass(self):
        spec = _spec(params={"select_models": ["a", "b"], "skip_tests": True}).validate()

        assert spec.params["skip_tests"] is True


class TestSpecSerialization:
    def test_round_trips_through_json(self):
        original = _spec(params={"select_models": ["x"]}, timeout_seconds=600)

        restored = TaskSpec.from_json(original.to_json())

        assert restored == original

    def test_version_is_carried(self):
        assert TaskSpec.from_json(_spec().to_json()).kind == "sqlmesh.plan"
        assert _spec().to_dict()["v"] == PROTOCOL_VERSION

    def test_future_version_is_rejected(self):
        payload = _spec().to_dict()
        payload["v"] = PROTOCOL_VERSION + 1

        with pytest.raises(TaskProtocolError, match="unsupported task spec version"):
            TaskSpec.from_dict(payload)

    def test_missing_required_field_is_reported_by_name(self):
        payload = _spec().to_dict()
        del payload["workspace_path"]

        with pytest.raises(TaskProtocolError, match="missing 'workspace_path'"):
            TaskSpec.from_dict(payload)

    def test_non_json_payload_is_rejected(self):
        with pytest.raises(TaskProtocolError, match="not valid JSON"):
            TaskSpec.from_json("{not json")

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(TaskProtocolError, match="must be a JSON object"):
            TaskSpec.from_json("[1, 2, 3]")


class TestTaskStatus:
    def test_terminal_states(self):
        assert TaskStatus.SUCCEEDED.is_terminal
        assert TaskStatus.FAILED.is_terminal
        assert TaskStatus.TIMED_OUT.is_terminal
        assert TaskStatus.CANCELLED.is_terminal

    def test_non_terminal_states(self):
        assert not TaskStatus.PENDING.is_terminal
        assert not TaskStatus.RUNNING.is_terminal


class TestTaskResult:
    def test_round_trips(self):
        original = TaskResult(
            task_id="task-1",
            status=TaskStatus.SUCCEEDED,
            result={"plan_id": "p1", "direct": ["m1"]},
            duration_ms=1234,
            exit_code=0,
        )

        restored = TaskResult.from_dict(original.to_dict())

        assert restored == original
        assert restored.succeeded

    def test_failure_helper(self):
        failed = TaskResult.failure("task-1", "task_failed", "boom", exit_code=1)

        assert not failed.succeeded
        assert failed.status is TaskStatus.FAILED
        assert failed.error_code == "task_failed"

    def test_failure_helper_can_set_a_non_default_status(self):
        timed_out = TaskResult.failure(
            "task-1", "task_timeout", "deadline", status=TaskStatus.TIMED_OUT
        )

        assert timed_out.status is TaskStatus.TIMED_OUT

    def test_unknown_status_is_rejected(self):
        with pytest.raises(TaskProtocolError, match="unknown task status"):
            TaskResult.from_dict({"task_id": "t", "status": "exploded"})

    def test_absent_exit_code_stays_none(self):
        restored = TaskResult.from_dict(
            {"task_id": "t", "status": "succeeded", "exit_code": None}
        )

        assert restored.exit_code is None


class TestStreamCodec:
    def test_encode_then_parse(self):
        line = encode_stream_line({"type": "progress", "stage": "loading"})

        assert line.startswith(STREAM_PREFIX)
        assert parse_stream_line(line) == {"type": "progress", "stage": "loading"}

    def test_ordinary_output_is_not_structured(self):
        assert parse_stream_line("Loading project...") is None
        assert parse_stream_line("") is None
        # A line merely mentioning the prefix word is still ordinary output.
        assert parse_stream_line("see @@DATACODE") is None

    def test_prefixed_line_with_broken_json_is_an_error(self):
        """Silently dropping it could hide a result."""
        with pytest.raises(TaskProtocolError, match="not valid JSON"):
            parse_stream_line(STREAM_PREFIX + "{broken")

    def test_prefixed_line_with_empty_payload_is_an_error(self):
        with pytest.raises(TaskProtocolError, match="empty payload"):
            parse_stream_line(STREAM_PREFIX)

    def test_prefixed_non_object_is_an_error(self):
        with pytest.raises(TaskProtocolError, match="must be a JSON object"):
            parse_stream_line(STREAM_PREFIX + "[1]")


class TestExtractResult:
    def _log(self, *extra: str) -> list[str]:
        return [
            "Loading project...",
            encode_stream_line(TaskProgress("task-1", "planning", 50).to_dict()),
            "sqlmesh: computing diff",
            *extra,
        ]

    def test_finds_the_result_among_ordinary_output(self):
        expected = TaskResult("task-1", TaskStatus.SUCCEEDED, {"plan_id": "p1"})
        lines = self._log(encode_stream_line(expected.to_dict()), "done.")

        assert extract_result(lines) == expected

    def test_returns_none_when_no_result_was_emitted(self):
        """A vehicle that died before reporting: the caller must synthesize one."""
        assert extract_result(self._log()) is None

    def test_last_result_wins(self):
        first = TaskResult("task-1", TaskStatus.FAILED, error_code="task_failed")
        final = TaskResult("task-1", TaskStatus.SUCCEEDED)
        lines = [
            encode_stream_line(first.to_dict()),
            encode_stream_line(final.to_dict()),
        ]

        assert extract_result(lines) == final

    def test_unparsable_lines_do_not_hide_a_valid_result(self):
        expected = TaskResult("task-1", TaskStatus.SUCCEEDED)
        lines = [
            encode_stream_line(expected.to_dict()),
            STREAM_PREFIX + "{truncated by a log rotation",
        ]

        assert extract_result(lines) == expected

    def test_accepts_a_tuple(self):
        expected = TaskResult("task-1", TaskStatus.SUCCEEDED)

        assert extract_result((encode_stream_line(expected.to_dict()),)) == expected


class TestTaskProgress:
    def test_round_trips(self):
        original = TaskProgress("task-1", "applying", 75, "3 of 4 models")

        assert TaskProgress.from_dict(original.to_dict()) == original

    def test_percent_is_optional(self):
        restored = TaskProgress.from_dict(
            {"task_id": "t", "stage": "loading", "percent": None}
        )

        assert restored.percent is None
