"""task-runner wire protocol — spec in, result out (design 04 §3).

The contract between workspace-runtime (which decides what must run and where)
and task-runner (which runs it inside a sandbox). Deliberately separate from
``executor_protocol``: that one is a long-lived request/response session with a
WARM process holding a SQLMesh Context, while this one describes a single
one-shot execution whose vehicle is created and destroyed around it.

Transport, and why it is stdout
-------------------------------
The spec reaches the runner as a JSON file (``/task/spec.json`` in the
container, a temp file for the local backend). The result travels back on
**stdout**, one line, behind :data:`STREAM_PREFIX`.

Design 04 §3.2 also sketches an HTTP callback to the runtime. That stays a
progress channel (``callback_url`` below) rather than the result channel, for
three reasons: the result must not depend on the network policy and scoped
token that only arrive in P6b; a Pod's logs survive the Pod, so a result read
from logs cannot be lost by a callback racing container teardown; and the local
development backend then uses the exact same path as production instead of a
second, less-tested one.

What must never be in a spec
----------------------------
Credentials. The spec is passed as a ConfigMap (not a Secret), it is written to
audit records, and it is visible to anything that can read the Pod definition.
Warehouse credentials reach the runner through the environment, injected from a
per-task Secret. :meth:`TaskSpec.validate` actively rejects
credential-looking keys rather than trusting every future caller to remember.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

# Bump together with a runner image release; the runner refuses a spec whose
# version it does not implement, since a silently-ignored field could mean a
# missing isolation constraint.
PROTOCOL_VERSION = 1

# Marks the structured lines in the runner's stdout. Long and unlikely enough
# that ordinary SQLMesh / dbt / shell output cannot collide with it.
STREAM_PREFIX = "@@DATACODE_TASK@@ "

# Task ids become Kubernetes object names (Job, ConfigMap, Secret), so they must
# be DNS-1123 subdomains. Enforced here rather than at the Kubernetes backend so
# the local backend rejects the same ids and dev catches the problem first.
_TASK_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# A task kind is "<domain>.<action>", matching isolation-profiles.yaml.
_TASK_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

# Upper bound on any single task. Above this a task is a background job that
# needs its own lifecycle, not a request the caller waits on.
MAX_TIMEOUT_SECONDS = 24 * 3600

# Substrings that mark a parameter as credential-like. Matched on the KEY only:
# matching values would reject legitimate SQL containing the word "password".
_CREDENTIAL_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "access_key",
    "api_key",
    "apikey",
)


class TaskProtocolError(ValueError):
    """A spec or result is malformed, or a spec carries something forbidden."""


class TaskStatus(str, Enum):
    """Lifecycle of one task, as recorded in ``task_records``."""

    PENDING = "pending"      # accepted, vehicle not started yet
    RUNNING = "running"      # vehicle started
    SUCCEEDED = "succeeded"
    FAILED = "failed"        # the task ran and reported failure
    TIMED_OUT = "timed_out"  # killed by the deadline
    CANCELLED = "cancelled"  # withdrawn before or during execution

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
        }


# ── Stable error codes ────────────────────────────────────────────────────
# Strings, not exception types: the runner may be a different image, and P6c
# swaps its execution vehicle again. Callers switch on these.
ERR_BAD_SPEC = "bad_spec"                    # spec rejected before execution
ERR_UNSUPPORTED_KIND = "unsupported_kind"    # this runner cannot do that kind
ERR_TASK_FAILED = "task_failed"              # the work itself failed
ERR_TIMEOUT = "task_timeout"
ERR_CANCELLED = "task_cancelled"
ERR_NO_RESULT = "no_result"                  # vehicle exited without a result
ERR_SUBMIT_FAILED = "submit_failed"          # could not create the vehicle
ERR_INTERNAL = "internal_error"
# The vehicle's memory limit killed the task: the runner caught MemoryError
# before the kernel did (local backend, RLIMIT_AS), or the submitter read the
# Pod's OOMKilled termination reason (container backend). Maps to the
# platform contract's SANDBOX_OOM (507) — distinct from a task that merely
# failed, because the user's next action differs (raise the limit vs fix code).
ERR_SANDBOX_OOM = "sandbox_oom"


@dataclass(frozen=True)
class TaskSpec:
    """Everything the runner needs, and nothing it must not have.

    Attributes:
        task_id: Unique id; also the name of the Kubernetes objects.
        kind: Task kind, e.g. ``sqlmesh.plan``. Selects the isolation profile
            and the handler inside the runner.
        workspace_path: Working copy path **as seen by the runner**. In a
            container that is the mount point (``/workspace``); for the local
            backend it is the real host path. The runner never derives it.
        environment: SQLMesh environment name this task acts on.
        tenant_id: Owning tenant. Audit and quota attribution only — the runner
            must not use it to resolve paths, since its mount is already
            narrowed to one working copy.
        workspace_id: Owning workspace, for audit.
        isolation_profile: Profile the submitter selected. Carried so the
            record shows what was actually applied, not what a later config
            edit would imply.
        params: Kind-specific arguments. Credential-like keys are rejected.
        timeout_seconds: Wall-clock budget. The backend also enforces it
            (``activeDeadlineSeconds``); the runner enforces it internally so
            the local backend behaves the same.
        actor_user_id: The user this task is attributed to, for audit.
        gateway: Warehouse gateway name to use, empty for the default.
        callback_url: Optional progress endpoint. Progress is
            best-effort; the result never travels this way.
    """

    task_id: str
    kind: str
    workspace_path: str
    environment: str = ""
    tenant_id: str = ""
    workspace_id: str = ""
    isolation_profile: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 1800
    actor_user_id: str = ""
    gateway: str = ""
    callback_url: str = ""

    def validate(self) -> "TaskSpec":
        """Check the spec, returning it so calls can be chained.

        Raises:
            TaskProtocolError: The spec is unusable, or carries a credential.
        """
        if not _TASK_ID_RE.match(self.task_id):
            raise TaskProtocolError(
                f"task_id {self.task_id!r} must be a DNS-1123 subdomain "
                f"(matching {_TASK_ID_RE.pattern}); it names Kubernetes objects"
            )
        if not _TASK_KIND_RE.match(self.kind):
            raise TaskProtocolError(
                f"kind {self.kind!r} must look like 'domain.action' "
                "to match isolation-profiles.yaml"
            )
        if not self.workspace_path:
            raise TaskProtocolError("workspace_path is required")
        if self.timeout_seconds <= 0 or self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise TaskProtocolError(
                f"timeout_seconds must be in 1..{MAX_TIMEOUT_SECONDS}, "
                f"got {self.timeout_seconds}"
            )
        self._reject_credentials(self.params)
        return self

    @staticmethod
    def _reject_credentials(params: Mapping[str, Any], path: str = "params") -> None:
        """Refuse credential-looking keys anywhere in the parameter tree.

        The spec travels through a ConfigMap and into audit records, neither of
        which is a secret store. Failing loudly here is the only way this stays
        true as new task kinds are added.
        """
        for key, value in params.items():
            lowered = str(key).lower()
            for marker in _CREDENTIAL_KEY_MARKERS:
                if marker in lowered:
                    raise TaskProtocolError(
                        f"{path}.{key} looks like a credential "
                        f"(matched {marker!r}); task specs are not secret — "
                        "inject credentials through the per-task Secret instead"
                    )
            if isinstance(value, Mapping):
                TaskSpec._reject_credentials(value, f"{path}.{key}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "task_id": self.task_id,
            "kind": self.kind,
            "workspace_path": self.workspace_path,
            "environment": self.environment,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "isolation_profile": self.isolation_profile,
            "params": dict(self.params),
            "timeout_seconds": self.timeout_seconds,
            "actor_user_id": self.actor_user_id,
            "gateway": self.gateway,
            "callback_url": self.callback_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        """Rebuild a spec, rejecting a version this code does not implement."""
        version = data.get("v", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise TaskProtocolError(
                f"unsupported task spec version {version!r}; "
                f"this build implements {PROTOCOL_VERSION}"
            )
        try:
            return cls(
                task_id=str(data["task_id"]),
                kind=str(data["kind"]),
                workspace_path=str(data["workspace_path"]),
                environment=str(data.get("environment", "")),
                tenant_id=str(data.get("tenant_id", "")),
                workspace_id=str(data.get("workspace_id", "")),
                isolation_profile=str(data.get("isolation_profile", "")),
                params=dict(data.get("params") or {}),
                timeout_seconds=int(data.get("timeout_seconds", 1800)),
                actor_user_id=str(data.get("actor_user_id", "")),
                gateway=str(data.get("gateway", "")),
                callback_url=str(data.get("callback_url", "")),
            )
        except KeyError as exc:
            raise TaskProtocolError(f"task spec is missing {exc.args[0]!r}") from None

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, payload: str) -> "TaskSpec":
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise TaskProtocolError(f"task spec is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise TaskProtocolError("task spec must be a JSON object")
        return cls.from_dict(data)


@dataclass(frozen=True)
class TaskResult:
    """The outcome of one task.

    ``result`` is kind-specific pure data (a PlanResultDTO dict, shell output,
    ...). Nothing executable crosses this boundary: that reduction is the point
    of running the work elsewhere.
    """

    task_id: str
    status: TaskStatus
    result: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    duration_ms: int = 0
    exit_code: Optional[int] = None

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "type": "result",
            "task_id": self.task_id,
            "status": self.status.value,
            "result": dict(self.result),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskResult":
        raw_status = str(data.get("status", ""))
        try:
            status = TaskStatus(raw_status)
        except ValueError:
            raise TaskProtocolError(
                f"unknown task status {raw_status!r}"
            ) from None
        return cls(
            task_id=str(data.get("task_id", "")),
            status=status,
            result=dict(data.get("result") or {}),
            error_code=str(data.get("error_code", "")),
            error_message=str(data.get("error_message", "")),
            duration_ms=int(data.get("duration_ms", 0) or 0),
            exit_code=(
                int(data["exit_code"]) if data.get("exit_code") is not None else None
            ),
        )

    @classmethod
    def failure(
        cls,
        task_id: str,
        code: str,
        message: str,
        *,
        status: TaskStatus = TaskStatus.FAILED,
        duration_ms: int = 0,
        exit_code: Optional[int] = None,
        result: Optional[Mapping[str, Any]] = None,
    ) -> "TaskResult":
        """Build a failure result. Used by both sides, hence the helper.

        ``result`` is not a contradiction: ``error_code`` says WHICH failure
        this is, while ``result`` carries what the caller needs to act on it —
        a dependency conflict's rewrite, say — which does not fit in a sentence
        and must not be recovered by parsing one.
        """
        return cls(
            task_id=task_id,
            status=status,
            result=dict(result) if result else {},
            error_code=code,
            error_message=message,
            duration_ms=duration_ms,
            exit_code=exit_code,
        )


@dataclass(frozen=True)
class TaskProgress:
    """A best-effort progress note from the runner.

    Never required: a task whose progress lines are all lost still produces a
    correct result.
    """

    task_id: str
    stage: str
    percent: Optional[int] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "type": "progress",
            "task_id": self.task_id,
            "stage": self.stage,
            "percent": self.percent,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskProgress":
        return cls(
            task_id=str(data.get("task_id", "")),
            stage=str(data.get("stage", "")),
            percent=(
                int(data["percent"]) if data.get("percent") is not None else None
            ),
            detail=str(data.get("detail", "")),
        )


# ── stdout stream codec ───────────────────────────────────────────────────


def encode_stream_line(payload: Mapping[str, Any]) -> str:
    """Render one structured line for the runner's stdout (no trailing newline)."""
    return STREAM_PREFIX + json.dumps(
        dict(payload), separators=(",", ":"), default=str
    )


def parse_stream_line(line: str) -> Optional[dict[str, Any]]:
    """Extract a structured payload from one stdout line.

    Returns None for ordinary output, so a caller can feed it every line of a
    log stream. A prefixed line with broken JSON is an error, though: the
    runner produced it, and silently dropping it could hide a result.
    """
    stripped = line.strip()
    if not stripped.startswith(STREAM_PREFIX.strip()):
        return None
    _, _, payload = stripped.partition(STREAM_PREFIX.strip())
    payload = payload.strip()
    if not payload:
        raise TaskProtocolError("structured stdout line has an empty payload")
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise TaskProtocolError(
            f"structured stdout line is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TaskProtocolError("structured stdout payload must be a JSON object")
    return parsed


def extract_result(lines: list[str] | tuple[str, ...]) -> Optional[TaskResult]:
    """Find the task result in a captured log stream.

    Scans from the END: a runner that (incorrectly) emitted several results
    should be read as reporting its final state. Unparsable prefixed lines are
    skipped here — unlike :func:`parse_stream_line`, this function's job is to
    find a result if one exists at all.
    """
    for line in reversed(list(lines)):
        try:
            payload = parse_stream_line(line)
        except TaskProtocolError:
            continue
        if payload and payload.get("type") == "result":
            return TaskResult.from_dict(payload)
    return None


__all__ = [
    "ERR_BAD_SPEC",
    "ERR_CANCELLED",
    "ERR_INTERNAL",
    "ERR_NO_RESULT",
    "ERR_SANDBOX_OOM",
    "ERR_SUBMIT_FAILED",
    "ERR_TASK_FAILED",
    "ERR_TIMEOUT",
    "ERR_UNSUPPORTED_KIND",
    "MAX_TIMEOUT_SECONDS",
    "PROTOCOL_VERSION",
    "STREAM_PREFIX",
    "TaskProgress",
    "TaskProtocolError",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
    "encode_stream_line",
    "extract_result",
    "parse_stream_line",
]
