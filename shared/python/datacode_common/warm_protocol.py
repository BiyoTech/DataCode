"""Warm task channel — a resident runner that accepts one task at a time.

Why this exists
---------------
The one-shot vehicle (``task_protocol``: spec file in, result line out, process
exits) pays a container start per task. For SQLMesh plans that is dwarfed by the
Context load; for ``agent.shell`` it IS the latency — a 10-30 s wait to run
``git status``. A resident runner amortizes both.

Relationship to the other two protocols
---------------------------------------
* ``task_protocol`` — WHAT runs (TaskSpec / TaskResult). Unchanged and reused
  verbatim: this module only adds a way to deliver a spec and collect a result
  over a socket instead of a file and a log line. A warm task and a one-shot
  task are the same task.
* ``executor_protocol`` — the warm SQLMesh Context session. Same 4-byte
  length-prefixed JSON framing, which is why the codec lives in
  :mod:`datacode_common.framing` and is imported by both rather than copied.

What this protocol adds, and why each piece is here
---------------------------------------------------
**Per-request credentials.** A resident Pod cannot carry one task's token in its
environment: it outlives the task. So the token and proxy settings travel INSIDE
the request, are applied for the duration of that task, and are dropped
afterwards. This keeps the P6b property that matters — a token is scoped to one
task and expires with it — without which "warm" would have meant "a long-lived
credential shared by every task in the Pod".

**A declared workspace.** The Pod mounts exactly one working copy (``subPath``,
the T1 closure). The request still names its workspace path, and the runner
REFUSES any request naming a different one. The mount already makes other copies
unreachable; this check makes a mis-routed request an explicit error instead of a
confusing "no such project".

**One task at a time.** A resident runner is not a work queue: concurrent tasks
in one Pod would share its memory limit, its CPU quota and its SQLMesh cache
directory. The server answers ``busy`` rather than queueing, and the pool on the
runtime side is what provides parallelism.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from datacode_common.framing import MAX_FRAME_BYTES, ProtocolError, decode, encode
from datacode_common.task_protocol import TaskResult, TaskSpec, TaskStatus

# Bumped together with the runner image, like task_protocol's own version: the
# runner refuses a request whose version it does not implement, because a
# silently ignored field could be an isolation constraint.
WARM_PROTOCOL_VERSION = 1

# Requests the resident runner answers.
OP_HELLO = "hello"          # server -> client, unsolicited, on connect
OP_PING = "ping"            # {} -> {"alive": true, "busy": bool}
OP_RUN = "run"              # {spec, credentials?} -> TaskResult dict
OP_SHUTDOWN = "shutdown"    # {} -> {"stopping": true}

# Errors specific to this channel. Task-level failures still use
# ``task_protocol``'s codes inside a TaskResult — a task that ran and failed is
# not a channel error.
ERR_BUSY = "runner_busy"
ERR_WRONG_WORKSPACE = "wrong_workspace"
ERR_UNKNOWN_OP = "unknown_op"
ERR_BAD_REQUEST = "bad_request"

__all__ = [
    "ERR_BAD_REQUEST",
    "ERR_BUSY",
    "ERR_UNKNOWN_OP",
    "ERR_WRONG_WORKSPACE",
    "MAX_FRAME_BYTES",
    "OP_HELLO",
    "OP_PING",
    "OP_RUN",
    "OP_SHUTDOWN",
    "ProtocolError",
    "WARM_PROTOCOL_VERSION",
    "decode",
    "encode",
    "make_error",
    "make_hello",
    "make_response",
    "make_run_request",
    "parse_run_request",
    "result_from_response",
]


def make_run_request(
    req_id: int,
    spec: TaskSpec,
    credentials: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Build a ``run`` request.

    Args:
        req_id: Correlation id, echoed in the response.
        spec: The task, exactly as the one-shot vehicle would receive it.
        credentials: Environment to apply FOR THIS TASK ONLY (task token, proxy
            settings). Kept out of ``spec`` deliberately: the spec is written to
            audit records and, in the one-shot vehicle, to a ConfigMap —
            ``TaskSpec.validate`` actively rejects credential-looking keys.
    """
    request: dict[str, Any] = {
        "v": WARM_PROTOCOL_VERSION,
        "id": req_id,
        "op": OP_RUN,
        "spec": spec.to_dict(),
    }
    if credentials:
        request["credentials"] = dict(credentials)
    return request


def parse_run_request(message: Mapping[str, Any]) -> tuple[TaskSpec, dict[str, str]]:
    """Validate a ``run`` request and split it into spec and credentials.

    Raises:
        ProtocolError: Version mismatch or a malformed spec. Refusing beats
            guessing: the caller built this frame, so it is the party that can
            fix it.
    """
    version = message.get("v")
    if version != WARM_PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported warm protocol version {version!r}; "
            f"this runner implements {WARM_PROTOCOL_VERSION}"
        )
    raw_spec = message.get("spec")
    if not isinstance(raw_spec, Mapping):
        raise ProtocolError("run request must carry a 'spec' object")
    spec = TaskSpec.from_dict(dict(raw_spec)).validate()

    raw_credentials = message.get("credentials") or {}
    if not isinstance(raw_credentials, Mapping):
        raise ProtocolError("'credentials' must be an object of env vars")
    return spec, {str(k): str(v) for k, v in raw_credentials.items()}


def make_hello(workspace_path: str, kinds: tuple[str, ...]) -> dict[str, Any]:
    """The greeting a resident runner sends on connect.

    Carries what the pool must verify before trusting the connection: which
    working copy this Pod is actually mounted on, and which kinds its image
    implements (a stale image is otherwise discovered as a failed task).
    """
    return {
        "v": WARM_PROTOCOL_VERSION,
        "ev": OP_HELLO,
        "data": {
            "workspace_path": workspace_path,
            "kinds": list(kinds),
            "protocol": WARM_PROTOCOL_VERSION,
        },
    }


def make_response(req_id: Optional[int], result: Mapping[str, Any]) -> dict[str, Any]:
    return {"v": WARM_PROTOCOL_VERSION, "id": req_id, "ok": True, "result": dict(result)}


def make_error(req_id: Optional[int], code: str, message: str) -> dict[str, Any]:
    return {
        "v": WARM_PROTOCOL_VERSION,
        "id": req_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def result_from_response(
    task_id: str, message: Mapping[str, Any]
) -> TaskResult:
    """Turn a response frame into a TaskResult.

    A channel-level error becomes a FAILED result rather than an exception, so
    the submitter branches on error codes exactly as it does for the one-shot
    vehicle. The one exception is deliberate: the pool needs to tell "this Pod
    is busy" apart from "the task failed", because the former is retryable on
    another Pod — so callers check for :data:`ERR_BUSY` before calling this.
    """
    if message.get("ok"):
        payload = message.get("result") or {}
        try:
            return TaskResult.from_dict(dict(payload))
        except Exception as exc:  # noqa: BLE001 - malformed result, still a result
            return TaskResult.failure(
                task_id,
                "no_result",
                f"resident runner returned an unreadable result: {exc}",
            )
    error = message.get("error") or {}
    return TaskResult.failure(
        task_id,
        str(error.get("code") or "internal_error"),
        str(error.get("message") or "resident runner reported an error"),
        status=TaskStatus.FAILED,
    )
