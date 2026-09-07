"""task-runner entry point — one task, then exit (design 04 §3, P6a).

Lifecycle:

1.  Read the spec from ``DATACODE_TASK_SPEC_PATH`` (a file, so the spec is
    neither visible in ``ps`` nor bounded by the environment size limit).
2.  Run the handler for its kind, streaming progress.
3.  Emit exactly one result line on stdout and exit.

Exit codes exist for the vehicle (a Kubernetes Job reports them, and a Pod that
never produced a result must still be classifiable), but the RESULT LINE is
authoritative. A caller that finds a result trusts it; a caller that finds none
synthesizes a failure from the exit code and the logs.

The process always emits a result — including for a bad spec or an internal
error — because "no result at all" forces the submitter to guess between a
crashed task, an OOM kill and an image that does not implement the kind.

Resident form (P6c warm pool)
-----------------------------
``--serve PORT`` keeps the process alive and takes tasks over a socket instead
(see :mod:`src.serve`). :func:`run_task` below is shared verbatim by both forms:
a warm task and a one-shot task must be the same task, or the warm path becomes
a second, less-tested execution semantics.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Optional

from datacode_common.task_protocol import (
    ERR_BAD_SPEC,
    ERR_INTERNAL,
    ERR_SANDBOX_OOM,
    ERR_TIMEOUT,
    TaskProtocolError,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from datacode_common.workspace_layout import sqlmesh_home, tool_home

from src import handlers
from src.sqlmesh_ops import SqlMeshOpError
from src.stream import emit_progress, emit_result, log

# Where the vehicle leaves the spec. A file rather than an env var: specs can be
# large, and environment contents leak through /proc to anything sharing the
# namespace.
SPEC_PATH_ENV = "DATACODE_TASK_SPEC_PATH"

# Used only to label a result when the spec itself could not be parsed, so the
# submitter can still correlate the failure with the vehicle it created.
TASK_ID_ENV = "DATACODE_TASK_ID"

# Exit codes. Distinct so a Job's status is informative even when the log stream
# is unavailable.
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_BAD_SPEC = 2
EXIT_TIMEOUT = 3
EXIT_INTERNAL = 4

_STATUS_EXIT_CODES = {
    TaskStatus.SUCCEEDED: EXIT_OK,
    TaskStatus.FAILED: EXIT_TASK_FAILED,
    TaskStatus.TIMED_OUT: EXIT_TIMEOUT,
    TaskStatus.CANCELLED: EXIT_TASK_FAILED,
}


def _read_spec(spec_path: Optional[str] = None) -> TaskSpec:
    """Load and validate the spec.

    Raises:
        TaskProtocolError: Missing, unreadable or invalid. Callers turn this
            into a ``bad_spec`` result rather than a traceback: the submitter
            wrote the spec, so it is the party that needs the message.
    """
    path = spec_path or os.environ.get(SPEC_PATH_ENV, "")
    if not path:
        raise TaskProtocolError(
            f"no task spec: set {SPEC_PATH_ENV} to a readable spec file"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            payload = handle.read()
    except OSError as exc:
        raise TaskProtocolError(f"cannot read task spec at {path}: {exc}") from exc
    return TaskSpec.from_json(payload).validate()


def _install_deadline(spec: TaskSpec) -> None:
    """Arm an in-process wall-clock alarm for the task's budget.

    The vehicle enforces the deadline too (``activeDeadlineSeconds``), but only
    by killing the Pod — which produces no result line. Firing first from inside
    lets the runner report ``timed_out`` properly, and gives the local
    development backend the same behaviour as Kubernetes.
    """
    try:
        import signal
    except ImportError:  # pragma: no cover - signal is always present on POSIX
        return

    def _on_deadline(_signum, _frame):
        raise TimeoutError(f"task exceeded its {spec.timeout_seconds}s budget")

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_deadline)
        signal.alarm(spec.timeout_seconds)


def _clear_deadline() -> None:
    try:
        import signal

        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    except ImportError:  # pragma: no cover
        pass


def _pin_tool_home(workspace_path: str) -> None:
    """Point every tool's home at the platform's state directory in the copy.

    Done from the SPEC, which is the only place that knows the path this
    vehicle actually mounted (the submitter rewrites it for a container), and
    done here rather than in each op because it must happen before the first
    ``import sqlmesh``: SQLMesh resolves its personal directory at import time.

    Two things follow from leaving it at the default:

    * ``~/.sqlmesh/user.yaml`` (a telemetry id, written even when telemetry is
      disabled) appears in the user's project tree as an untracked file;
    * ``~/.sqlmesh/config.yaml`` is a PERSONAL SQLMesh config, merged over the
      project's — so a file dropped at the default location repoints gateways
      and state storage of the run. Pinning it inside the platform-owned tree
      keeps the project config the authoritative one.

    In a container HOME belongs to the image (``/nonexistent`` for the runner's
    unprivileged uid, on a read-only root filesystem), so this also removes a
    failure mode where SQLMesh cannot create its directory at all.

    The directory is created here, not by the runtime: this runs in EVERY
    vehicle (one-shot container, warm runner, local child), while the runtime
    process is not even involved in the warm path after the Pod starts. Tools
    differ on whether they create their own home — SQLMesh does, ``git`` does
    not — so the one that does not must still find it.
    """
    if not workspace_path:
        return
    home = tool_home(workspace_path)
    os.environ["HOME"] = home
    os.environ["SQLMESH_HOME"] = sqlmesh_home(workspace_path)
    try:
        os.makedirs(home, exist_ok=True)
    except OSError as exc:
        # Not fatal: a read-only mount is a legitimate sandbox shape, and the
        # tools that only READ a home are unaffected. The task's own failure —
        # if any — will name the tool, which is more useful than this.
        log(f"could not create tool home {home}: {exc}")


def run_task(spec: TaskSpec) -> TaskResult:
    """Execute one validated spec and produce its result.

    Never raises: every failure mode becomes a result, because the submitter
    distinguishes outcomes by error code, not by exception type.
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def _progress(stage: str, percent: Optional[int], detail: str) -> None:
        emit_progress(spec.task_id, stage, percent, detail)

    _pin_tool_home(spec.workspace_path)
    _install_deadline(spec)
    try:
        payload = handlers.run(
            spec.kind,
            spec.workspace_path,
            spec.params,
            gateway=spec.gateway,
            environment=spec.environment,
            progress=_progress,
        )
        return TaskResult(
            task_id=spec.task_id,
            status=TaskStatus.SUCCEEDED,
            result=payload,
            duration_ms=_elapsed_ms(),
            exit_code=EXIT_OK,
        )
    except TimeoutError as exc:
        return TaskResult.failure(
            spec.task_id,
            ERR_TIMEOUT,
            str(exc),
            status=TaskStatus.TIMED_OUT,
            duration_ms=_elapsed_ms(),
            exit_code=EXIT_TIMEOUT,
        )
    except SqlMeshOpError as exc:
        # Expected failure shape: a stable code the submitter can branch on.
        return TaskResult.failure(
            spec.task_id,
            exc.code,
            str(exc),
            duration_ms=_elapsed_ms(),
            exit_code=EXIT_TASK_FAILED,
        )
    except MemoryError:
        # Report before anything else allocates. cgroup limits usually kill the
        # container outright, but when Python notices first this is far more
        # actionable than an exit code alone.
        return TaskResult.failure(
            spec.task_id,
            ERR_SANDBOX_OOM,
            "the task exhausted its memory limit",
            duration_ms=_elapsed_ms(),
            exit_code=EXIT_INTERNAL,
        )
    except Exception as exc:  # noqa: BLE001 — the outermost task boundary
        log(traceback.format_exc())
        return TaskResult.failure(
            spec.task_id,
            ERR_INTERNAL,
            f"{type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(),
            exit_code=EXIT_INTERNAL,
        )
    finally:
        _clear_deadline()


def main(argv: Optional[list[str]] = None) -> int:
    """Run the task named by the environment (or by argv[1]) and report it.

    ``--serve PORT`` switches to the RESIDENT form (warm pool, P6c): the process
    stays alive and accepts tasks over a socket. Same handlers, same result
    shape — only the delivery differs (see :mod:`src.serve`).
    """
    args = list(argv if argv is not None else sys.argv[1:])

    if args and args[0] == "--serve":
        from src import serve as serve_module

        return serve_module.main(args[1:])

    spec_path = args[0] if args else None

    try:
        spec = _read_spec(spec_path)
    except TaskProtocolError as exc:
        emit_result(
            TaskResult.failure(
                os.environ.get(TASK_ID_ENV, "unknown"),
                ERR_BAD_SPEC,
                str(exc),
                exit_code=EXIT_BAD_SPEC,
            )
        )
        return EXIT_BAD_SPEC

    log(f"task {spec.task_id} kind={spec.kind} profile={spec.isolation_profile or '?'}")
    result = run_task(spec)
    emit_result(result)
    return _STATUS_EXIT_CODES.get(result.status, EXIT_INTERNAL)


def cli() -> None:
    """Console-script wrapper."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
