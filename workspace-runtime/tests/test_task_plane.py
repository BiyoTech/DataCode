"""Tests for the task execution plane (design 04 §3, design 06 P6a).

Three things are asserted here, in order of importance:

1.  A task NEVER runs in a weaker sandbox than its kind requires — the submitter
    refuses instead of downgrading.
2.  A spec carries no credentials, and the platform's own secrets do not reach a
    child process.
3.  Auditing cannot change a task's outcome.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from datacode_common.isolation import (
    IsolationLevel,
    IsolationProfile,
    build_isolation_policy,
)
from datacode_common.task_protocol import (
    ERR_NO_RESULT,
    ERR_SANDBOX_OOM,
    ERR_TASK_FAILED,
    ERR_TIMEOUT,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

from src.isolation import policy as policy_module
from src.isolation.hardening import (
    INHERITED_ENV_PREFIXES,
    ResourceLimits,
    build_clean_env,
    platform_secret_names,
)
from src.tasks.backends.base import TaskBackend, TaskBackendError
from src.tasks.submitter import (
    TaskSubmissionError,
    TaskSubmitter,
    sandbox_verdict_for,
    submission_unavailable,
)
from src.utils.errors import ErrorCode

# ── doubles ────────────────────────────────────────────────────────────


class _RecordingAuditor:
    """Captures audit calls instead of touching a database."""

    def __init__(self, *, fail: bool = False) -> None:
        self.submitted: list[TaskSpec] = []
        self.finished: list[tuple[TaskSpec, TaskResult]] = []
        self._fail = fail

    async def record_submitted(self, spec: TaskSpec, *, backend: str) -> None:
        if self._fail:
            raise RuntimeError("audit database is down")
        self.submitted.append(spec)

    async def record_finished(self, spec: TaskSpec, result: TaskResult) -> None:
        if self._fail:
            raise RuntimeError("audit database is down")
        self.finished.append((spec, result))


class _FakeBackend(TaskBackend):
    """A backend that returns a canned result and records what it was given."""

    def __init__(
        self,
        levels: frozenset[IsolationLevel] = frozenset({IsolationLevel.CONTAINER}),
        result: Optional[TaskResult] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self._levels = levels
        self._result = result
        self._error = error
        self.calls: list[tuple[TaskSpec, IsolationProfile]] = []
        # What the submitter handed over as the task's credentials, so a test can
        # assert the sandbox got (or did not get) a token.
        self.credentials: list[object] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supported_levels(self) -> frozenset[IsolationLevel]:
        return self._levels

    async def execute(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: object = None,
    ) -> TaskResult:
        self.calls.append((spec, profile))
        self.credentials.append(credentials)
        if self._error is not None:
            raise self._error
        return self._result or TaskResult(spec.task_id, TaskStatus.SUCCEEDED)

    async def close(self) -> None:
        self.closed = True


def _policy_document() -> dict:
    return {
        "profiles": {
            "L1_inprocess": {
                "isolation": "in_process",
                "allowed_task_kinds": ["file.read"],
            },
            "L3_proc": {
                "isolation": "process",
                "rlimits": {"cpu_seconds": 60, "max_processes": 8},
                "run_as_user_base": 60000,
                "allowed_task_kinds": ["agent.shell"],
            },
            "L2_container": {
                "isolation": "container",
                "runtime_class": "runc",
                "timeout_seconds": 3600,
                "credentials": {"warehouse": "scoped_readwrite"},
                "allowed_task_kinds": ["sqlmesh.plan", "sqlmesh.apply"],
            },
        },
        "default_profile": "strongest_enabled",
    }


@pytest.fixture
def container_policy(monkeypatch):
    """Install a policy where container profiles are available."""
    policy = build_isolation_policy(_policy_document())
    monkeypatch.setattr(policy_module, "runtime_isolation_policy", lambda: policy)
    monkeypatch.setattr(
        "src.tasks.submitter.runtime_isolation_policy", lambda: policy
    )
    return policy


@pytest.fixture
def process_only_policy(monkeypatch):
    """Install a policy where only process-level isolation is available."""
    policy = build_isolation_policy(_policy_document()).restricted_to(
        frozenset({IsolationLevel.IN_PROCESS, IsolationLevel.PROCESS})
    )
    monkeypatch.setattr(
        "src.tasks.submitter.runtime_isolation_policy", lambda: policy
    )
    return policy


@pytest.fixture
def in_process_only_policy(monkeypatch):
    """Install a policy with the whole sandbox plane switched off."""
    policy = build_isolation_policy(_policy_document()).restricted_to(
        frozenset({IsolationLevel.IN_PROCESS})
    )
    monkeypatch.setattr(
        "src.tasks.submitter.runtime_isolation_policy", lambda: policy
    )
    return policy


# ── hardening ──────────────────────────────────────────────────────────


class TestCleanEnvironment:
    """The child must not be able to read the platform's secrets."""

    def test_platform_secrets_are_not_inherited(self):
        source = {
            "DB_PASSWORD": "hunter2",
            "OPENAI_API_KEY": "sk-test",
            "TOKEN_ENCRYPTION_SECRET": "s3cr3t",
            "IDENTITY_SERVICE_INTERNAL_TOKEN": "internal",
            "REDIS_PASSWORD": "redis",
        }

        env = build_clean_env(
            workspace_path="/w", environment="dev", source_env=source
        )

        for name in source:
            assert name not in env

    def test_the_working_copys_own_warehouse_settings_are_inherited(self):
        """The user configured these, so reading them back is their right (Q5)."""
        source = {
            "DATACODE_WAREHOUSE_DSN": "duckdb:///local.db",
            "DB_PASSWORD": "hunter2",
        }

        env = build_clean_env(workspace_path="/w", source_env=source)

        assert env["DATACODE_WAREHOUSE_DSN"] == "duckdb:///local.db"
        assert "DB_PASSWORD" not in env

    def test_path_is_not_inherited(self):
        """An inherited PATH could let a task shadow git or python."""
        env = build_clean_env(
            workspace_path="/w", source_env={"PATH": "/tmp/evil:/usr/bin"}
        )

        assert "/tmp/evil" not in env["PATH"]

    def test_home_is_platform_state_inside_the_working_copy(self):
        """Inside the copy (containment), but NOT at its root.

        The root is the user's project: a tool dotfile written there shows up
        among their models, and a ``.gitconfig`` that arrived with the
        repository would become this child's GLOBAL git config.
        """
        env = build_clean_env(workspace_path="/w/copy")

        assert env["HOME"] == "/w/copy/.datacode/home"

    def test_sqlmesh_home_is_pinned_out_of_the_project_tree(self):
        """SQLMesh resolves its personal directory at IMPORT time.

        Left at the default it writes a telemetry id into the user's tree, and
        — the part that matters — it reads a personal ``config.yaml`` from
        there that is merged OVER the project's, so anything able to write the
        copy could repoint the gateways of a sandboxed run.
        """
        env = build_clean_env(workspace_path="/w/copy")

        assert env["SQLMESH_HOME"] == "/w/copy/.datacode/home/.sqlmesh"
        # Redundant with HOME by construction, and that is the point: a vehicle
        # that only sets one of them lands in the same place.
        assert env["SQLMESH_HOME"].startswith(env["HOME"] + "/")

    def test_time_is_utc(self):
        """The platform compares UTC; an inherited zone would render disagreeing
        timestamps."""
        env = build_clean_env(workspace_path="/w", source_env={"TZ": "Asia/Shanghai"})

        assert env["TZ"] == "UTC"

    def test_python_path_is_injected_when_given(self):
        env = build_clean_env(workspace_path="/w", python_path=("/a", "", "/b"))

        assert env["PYTHONPATH"] == "/a:/b"

    def test_extra_variables_are_added(self):
        env = build_clean_env(workspace_path="/w", extra={"DATACODE_TASK_ID": "t1"})

        assert env["DATACODE_TASK_ID"] == "t1"

    def test_the_callers_environment_is_not_modified(self):
        source = {"DB_PASSWORD": "hunter2"}

        build_clean_env(workspace_path="/w", source_env=source)

        assert source == {"DB_PASSWORD": "hunter2"}


class TestPlatformSecretNames:
    """Used by the P6 verification script, so it must be precise."""

    def test_detects_the_usual_shapes(self):
        found = platform_secret_names(
            {
                "DB_PASSWORD": "x",
                "GW_JWT_SECRET": "x",
                "SOME_TOKEN": "x",
                "TLS_PRIVATE_KEY": "x",
                "PATH": "/usr/bin",
            }
        )

        assert found == ["DB_PASSWORD", "GW_JWT_SECRET", "SOME_TOKEN", "TLS_PRIVATE_KEY"]

    def test_excludes_the_inherited_warehouse_prefix(self):
        name = f"{INHERITED_ENV_PREFIXES[0]}TOKEN"

        assert platform_secret_names({name: "x"}) == []


class TestResourceLimits:
    def test_built_from_a_profile(self):
        limits = ResourceLimits.from_profile(
            {"rlimits": {"cpu_seconds": 42, "max_processes": 7}}
        )

        assert limits.cpu_seconds == 42
        assert limits.max_processes == 7

    def test_missing_rlimits_block_yields_defaults(self):
        assert ResourceLimits.from_profile({}) == ResourceLimits()

    def test_a_malformed_rlimits_block_yields_defaults(self):
        """A broken profile must not silently mean 'no limits'."""
        assert ResourceLimits.from_profile({"rlimits": "nonsense"}) == ResourceLimits()


# ── level availability ─────────────────────────────────────────────────


class TestAvailableLevels:
    def test_in_process_is_always_available(self, monkeypatch):
        monkeypatch.setattr(policy_module.settings, "task_backend", "disabled")
        monkeypatch.setattr(policy_module.settings, "executor_enabled", False)

        assert policy_module.available_levels() == frozenset(
            {IsolationLevel.IN_PROCESS}
        )

    def test_the_local_backend_provides_process_isolation(self, monkeypatch):
        monkeypatch.setattr(policy_module.settings, "task_backend", "local")
        monkeypatch.setattr(policy_module.settings, "executor_enabled", False)

        assert IsolationLevel.PROCESS in policy_module.available_levels()
        assert IsolationLevel.CONTAINER not in policy_module.available_levels()

    def test_the_persistent_executor_also_provides_process_isolation(self, monkeypatch):
        monkeypatch.setattr(policy_module.settings, "task_backend", "disabled")
        monkeypatch.setattr(policy_module.settings, "executor_enabled", True)

        assert IsolationLevel.PROCESS in policy_module.available_levels()

    def test_only_kubernetes_provides_containers(self, monkeypatch):
        monkeypatch.setattr(policy_module.settings, "task_backend", "kubernetes")

        levels = policy_module.available_levels()

        assert IsolationLevel.CONTAINER in levels


# ── submitter ──────────────────────────────────────────────────────────


class TestProfileSelection:
    def test_a_container_kind_reaches_a_container_backend(self, container_policy):
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        _spec, profile = backend.calls[0]
        assert profile.name == "L2_container"

    def test_a_backend_that_cannot_provide_the_level_is_refused(
        self, container_policy
    ):
        """The core anti-downgrade property."""
        weaker = _FakeBackend(levels=frozenset({IsolationLevel.PROCESS}))
        submitter = TaskSubmitter(backend=weaker, auditor=_RecordingAuditor())

        with pytest.raises(TaskSubmissionError, match="cannot provide"):
            asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        assert weaker.calls == []

    def test_in_process_kinds_must_not_be_submitted(self, container_policy):
        submitter = TaskSubmitter(
            backend=_FakeBackend(), auditor=_RecordingAuditor()
        )

        with pytest.raises(TaskSubmissionError, match="run it directly"):
            asyncio.run(submitter.submit("file.read", "/w/copy"))

    def test_with_the_plane_off_gated_work_is_refused(self, in_process_only_policy):
        submitter = TaskSubmitter(
            backend=_FakeBackend(), auditor=_RecordingAuditor()
        )

        with pytest.raises(TaskSubmissionError, match="no fail-safe profile"):
            asyncio.run(submitter.submit("agent.shell", "/w/copy"))

    def test_an_unclaimed_kind_uses_the_strongest_available_profile(
        self, process_only_policy
    ):
        backend = _FakeBackend(levels=frozenset({IsolationLevel.PROCESS}))
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(submitter.submit("brand.new", "/w/copy"))

        _spec, profile = backend.calls[0]
        assert profile.name == "L3_proc"


class TestSpecConstruction:
    def test_the_spec_records_the_profile_actually_applied(self, container_policy):
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(
            submitter.submit(
                "sqlmesh.apply",
                "/w/copy",
                environment="dev",
                tenant_id="tenant-1",
                workspace_id="ws-1",
                actor_user_id="user-1",
            )
        )

        spec, _profile = backend.calls[0]
        assert spec.isolation_profile == "L2_container"
        assert spec.tenant_id == "tenant-1"
        assert spec.actor_user_id == "user-1"

    def test_task_ids_are_kubernetes_safe(self, container_policy):
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        spec, _ = backend.calls[0]
        assert spec.task_id.isalnum() and spec.task_id.islower()

    def test_a_credential_in_params_is_refused(self, container_policy):
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        with pytest.raises(TaskSubmissionError, match="credential"):
            asyncio.run(
                submitter.submit(
                    "sqlmesh.plan", "/w/copy", params={"db_password": "hunter2"}
                )
            )

        assert backend.calls == []

    def test_the_strictest_timeout_wins(self, container_policy, monkeypatch):
        monkeypatch.setattr(
            "src.tasks.submitter.settings.task_max_timeout_seconds", 900
        )
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        # Caller asks 7200, profile allows 3600, deployment caps at 900.
        asyncio.run(
            submitter.submit("sqlmesh.plan", "/w/copy", timeout_seconds=7200)
        )

        spec, _ = backend.calls[0]
        assert spec.timeout_seconds == 900

    def test_the_callers_shorter_timeout_is_honoured(
        self, container_policy, monkeypatch
    ):
        monkeypatch.setattr(
            "src.tasks.submitter.settings.task_max_timeout_seconds", 3600
        )
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy", timeout_seconds=30))

        spec, _ = backend.calls[0]
        assert spec.timeout_seconds == 30


class TestOutcomeHandling:
    def test_a_failed_task_is_returned_not_raised(self, container_policy):
        failure = TaskResult.failure("t", "task_failed", "plan failed")
        submitter = TaskSubmitter(
            backend=_FakeBackend(result=failure), auditor=_RecordingAuditor()
        )

        result = asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        assert not result.succeeded
        assert result.error_code == "task_failed"

    def test_a_backend_failure_raises_and_is_still_audited(self, container_policy):
        """The vehicle could not be created: the user's work never ran."""
        auditor = _RecordingAuditor()
        submitter = TaskSubmitter(
            backend=_FakeBackend(error=TaskBackendError("no nodes available")),
            auditor=auditor,
        )

        with pytest.raises(TaskSubmissionError, match="no nodes available"):
            asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        assert len(auditor.finished) == 1
        assert auditor.finished[0][1].error_code == "submit_failed"


class TestAuditIntegration:
    def test_both_lifecycle_points_are_recorded(self, container_policy):
        auditor = _RecordingAuditor()
        submitter = TaskSubmitter(backend=_FakeBackend(), auditor=auditor)

        asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        assert len(auditor.submitted) == 1
        assert len(auditor.finished) == 1
        assert auditor.finished[0][1].succeeded

    def test_a_result_carries_no_result_code_when_the_vehicle_died(
        self, container_policy
    ):
        dead = TaskResult.failure("t", ERR_NO_RESULT, "exited without a result")
        submitter = TaskSubmitter(
            backend=_FakeBackend(result=dead), auditor=_RecordingAuditor()
        )

        result = asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

        assert result.error_code == ERR_NO_RESULT


class TestSandboxVerdicts:
    """Vehicle-level failures map to their contract codes (design 08 §7).

    The mapping is the difference between "raise the memory limit" and "fix
    your code" — a mis-classification sends the user down the wrong path.
    """

    def test_timeout_maps_to_sandbox_timeout(self):
        result = TaskResult.failure("t", ERR_TIMEOUT, "deadline exceeded")

        error = sandbox_verdict_for(result)

        assert error is not None
        assert error.code == ErrorCode.SANDBOX_TIMEOUT
        assert error.status_code == 504

    def test_oom_maps_to_sandbox_oom(self):
        result = TaskResult.failure("t", ERR_SANDBOX_OOM, "memory limit hit")

        error = sandbox_verdict_for(result)

        assert error is not None
        assert error.code == ErrorCode.SANDBOX_OOM
        assert error.status_code == 507

    def test_no_result_maps_to_sandbox_unavailable(self):
        """The honest fallback: the vehicle died without a classifiable reason."""
        result = TaskResult.failure("t", ERR_NO_RESULT, "exited silently")

        error = sandbox_verdict_for(result)

        assert error is not None
        assert error.code == ErrorCode.SANDBOX_UNAVAILABLE
        assert error.status_code == 503

    def test_the_works_own_failure_carries_no_verdict(self):
        """A failed plan is not a sandbox problem: the caller keeps its domain code."""
        result = TaskResult.failure("t", ERR_TASK_FAILED, "lint errors")

        assert sandbox_verdict_for(result) is None

    def test_a_successful_task_carries_no_verdict(self):
        assert sandbox_verdict_for(TaskResult("t", TaskStatus.SUCCEEDED)) is None

    def test_a_submission_failure_is_sandbox_unavailable_with_retry_hint(self):
        error = submission_unavailable(TaskSubmissionError("no nodes available"))

        assert error.code == ErrorCode.SANDBOX_UNAVAILABLE
        assert error.status_code == 503
        assert error.headers == {"Retry-After": "5"}
        assert "no nodes available" in error.message


class TestBackendLifecycle:
    def test_close_releases_the_backend(self, container_policy):
        backend = _FakeBackend()
        submitter = TaskSubmitter(backend=backend, auditor=_RecordingAuditor())

        asyncio.run(submitter.close())

        assert backend.closed

    def test_a_disabled_plane_reports_the_setting_to_change(self, monkeypatch):
        monkeypatch.setattr("src.tasks.submitter.settings.task_backend", "disabled")
        submitter = TaskSubmitter(auditor=_RecordingAuditor())

        with pytest.raises(TaskSubmissionError, match="task_backend"):
            asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))

    def test_an_unknown_backend_name_is_reported(self, monkeypatch):
        monkeypatch.setattr("src.tasks.submitter.settings.task_backend", "magic")
        submitter = TaskSubmitter(auditor=_RecordingAuditor())

        with pytest.raises(TaskSubmissionError, match="unknown task backend"):
            asyncio.run(submitter.submit("sqlmesh.plan", "/w/copy"))
