"""Resident task server — the warm-pool form of the runner (design 06 P6c).

``python -m src.main --serve PORT`` keeps the process alive and accepts tasks
over a socket instead of reading one spec file and exiting. Everything about
WHAT runs is unchanged: the same :func:`src.main.run_task`, the same handlers,
the same ``TaskResult``. Only the delivery changes, which is what makes a warm
task and a one-shot task the same task.

The three properties that make this safe to keep alive
------------------------------------------------------
**Credentials are per task, never per Pod.** A resident Pod outlives any single
task, so it must not hold a task's token in its environment. The token arrives
inside each request, is applied to ``os.environ`` for the duration of that task
only, and is removed afterwards — including when the task fails. A Pod that has
just finished a task holds no warehouse credential at all.

**The workspace is fixed by the mount, and re-checked here.** The Pod mounts one
working copy (``subPath``); a request naming a different path is refused rather
than attempted. The mount already makes other copies unreachable — this turns a
mis-routed request into a clear error instead of "project not found".

**One task at a time.** Concurrent tasks in one Pod would share its memory
limit, CPU quota and SQLMesh cache directory. The server answers ``busy``; the
pool on the runtime side provides parallelism by holding several Pods.

What deliberately did NOT change
--------------------------------
The deadline still fires in-process per task (``run_task`` arms it), so a task
that overruns is reported as ``timed_out`` rather than taking the Pod with it.
The Pod is not restarted between tasks, so a task that corrupts its own
interpreter state (a segfaulting C extension, a leaked thread) would affect the
next one — which is why the pool retires a Pod after a configurable number of
tasks rather than keeping it forever.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any, Optional

from datacode_common.framing import ProtocolError, encode, read_frame_sync
from datacode_common.task_protocol import TaskResult, TaskSpec
from datacode_common.warm_protocol import (
    ERR_BAD_REQUEST,
    ERR_BUSY,
    ERR_UNKNOWN_OP,
    ERR_WRONG_WORKSPACE,
    OP_PING,
    OP_RUN,
    OP_SHUTDOWN,
    make_error,
    make_hello,
    make_response,
    parse_run_request,
)

from src import handlers
from src.stream import log

# Listen backlog. Small on purpose: the pool dials one connection per Pod, and a
# queue of pending connections here would hide a routing bug behind latency.
_BACKLOG = 4


class _SocketStream:
    """File-like adapter so the shared framing codec can read a socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def read(self, size: int) -> bytes:
        return self._connection.recv(size)

    def write(self, data: bytes) -> None:
        self._connection.sendall(data)


def _declared_workspace() -> str:
    """The one working copy this Pod is mounted on."""
    return os.environ.get("DATACODE_WORKSPACE_PATH", "")


class ResidentRunner:
    """Serves tasks over one connection at a time."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace_path = workspace_path
        self._busy = False
        self._stopping = False
        self._tasks_served = 0

    @property
    def tasks_served(self) -> int:
        return self._tasks_served

    def handle(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Dispatch one request frame and build its response."""
        req_id = message.get("id")
        op = message.get("op")

        if op == OP_PING:
            return make_response(req_id, {"alive": True, "busy": self._busy})
        if op == OP_SHUTDOWN:
            self._stopping = True
            return make_response(req_id, {"stopping": True})
        if op != OP_RUN:
            return make_error(req_id, ERR_UNKNOWN_OP, f"unknown op {op!r}")

        if self._busy:
            # Not queued: see the module docstring. The pool retries elsewhere.
            return make_error(
                req_id, ERR_BUSY, "this runner is already executing a task"
            )

        try:
            spec, credentials = parse_run_request(message)
        except (ProtocolError, ValueError) as exc:
            return make_error(req_id, ERR_BAD_REQUEST, str(exc))

        if spec.workspace_path != self._workspace_path:
            return make_error(
                req_id,
                ERR_WRONG_WORKSPACE,
                f"this runner serves {self._workspace_path!r}, "
                f"the request names {spec.workspace_path!r}",
            )

        self._busy = True
        try:
            result = self._run(spec, credentials)
        finally:
            self._busy = False
            self._tasks_served += 1
        return make_response(req_id, result.to_dict())

    def _run(self, spec: TaskSpec, credentials: dict[str, str]) -> TaskResult:
        """Execute one task with its credentials applied for that task only."""
        from src.main import run_task

        restore = _apply_credentials(credentials)
        try:
            log(f"warm task {spec.task_id} kind={spec.kind}")
            return run_task(spec)
        finally:
            # Unconditionally: a failed task must not leave its token behind for
            # the next one, which is the whole reason credentials are per task.
            _apply_credentials(restore, replace=True)

    @property
    def stopping(self) -> bool:
        return self._stopping


def _apply_credentials(
    credentials: dict[str, str], *, replace: bool = False
) -> dict[str, str]:
    """Apply env vars, returning what is needed to undo the change.

    The undo map uses ``""`` to mean "was not set", which :func:`_apply_credentials`
    with ``replace=True`` turns back into a deletion. Doing it this way keeps the
    caller from having to distinguish "restore" from "clear".
    """
    previous: dict[str, str] = {}
    for key, value in credentials.items():
        previous[key] = os.environ.get(key, "")
        if replace and not value:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return previous


def serve(port: int, workspace_path: Optional[str] = None) -> int:
    """Accept and serve task requests until shutdown.

    Returns the process exit code. A connection ending is NOT a reason to exit:
    the pool may reconnect (a transport hiccup should not cost the Pod, whose
    value is precisely that it is already warm). Only an explicit ``shutdown``
    or a signal ends the process.
    """
    resident = ResidentRunner(workspace_path or _declared_workspace())
    if not resident._workspace_path:  # noqa: SLF001 - own object
        log("refusing to serve: DATACODE_WORKSPACE_PATH is not set")
        return 2

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))  # noqa: S104 - the Pod's own namespace
    server.listen(_BACKLOG)
    log(f"resident runner listening on {port} for {resident._workspace_path}")  # noqa: SLF001

    try:
        while not resident.stopping:
            connection, _peer = server.accept()
            try:
                _serve_connection(resident, connection)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass
    finally:
        server.close()
    log(f"resident runner stopping after {resident.tasks_served} task(s)")
    return 0


def _serve_connection(resident: ResidentRunner, connection: socket.socket) -> None:
    """Serve one connection: hello, then request/response until EOF."""
    stream = _SocketStream(connection)
    stream.write(
        encode(make_hello(resident._workspace_path, handlers.supported_kinds()))  # noqa: SLF001
    )

    while not resident.stopping:
        try:
            message = read_frame_sync(stream)
        except ProtocolError as exc:
            # A malformed frame desynchronizes the stream, so the connection
            # cannot be trusted for further frames; report and drop it.
            try:
                stream.write(encode(make_error(None, ERR_BAD_REQUEST, str(exc))))
            except OSError:
                pass
            return
        except OSError:
            return
        if not message:  # clean EOF
            return

        response = resident.handle(message)
        if response is None:
            continue
        try:
            stream.write(encode(response))
        except OSError:
            return


def main(argv: Optional[list[str]] = None) -> int:
    """``--serve PORT`` entry point, wired from :mod:`src.main`."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        log("usage: --serve PORT")
        return 2
    try:
        port = int(args[0])
    except ValueError:
        log(f"invalid port {args[0]!r}")
        return 2
    return serve(port)


__all__ = ["ResidentRunner", "main", "serve"]
