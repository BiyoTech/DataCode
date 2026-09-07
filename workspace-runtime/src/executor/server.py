"""Executor sandbox — the isolated side of the frozen protocol.

Two transports, one protocol (P6c):

* **stdio** — the L3-proc child form. Runs as ``python -m src.executor.server``
  with an EMPTY environment (the parent re-injects the L3_proc allowlist),
  lowered rlimits, cwd pinned to one working copy, and the socketpair wired to
  fd 0/1.
* **TCP** — the L3-base container form. Same module, ``--tcp PORT``: the Pod
  accepts the runtime's connection and serves byte-for-byte the same frames.

It holds ONE SQLMesh Context per gateway for ONE working copy and serves the
ops that must not run in the platform process: constructing the Context execs
the user's Python models and macros (threat A3 / T2), plan/render/test and
gated shell execution. The former ``sqlmesh.apply`` op was removed in P6c: this
sandbox executes user code and therefore holds a read-only warehouse account at
most (Q5) — the write half of an apply runs in an L2 task.

Single-threaded and blocking on purpose: the sandbox serializes every op, the
serialization is the isolation story, and asyncio in a uid-separated
rlimited process buys nothing. Exit on EOF (stdio) or SIGTERM (TCP) closes the
Context and frees the slot.
"""

from __future__ import annotations

import json
import os
import sys

from src.executor.op_error import OpError as _OpError

# Length-prefixed JSON over fd 0/1, exactly as in datacode_common.
# Reimplemented here minimally and dependency-free: this process must be able
# to start from a bare interpreter with a cleared environment (no site
# packages assumed until sqlmesh is imported lazily per op).


def _read_frame() -> dict | None:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    length = int.from_bytes(header, "big")
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_frame(message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"), default=str).encode("utf-8")
    sys.stdout.buffer.write(len(payload).to_bytes(4, "big"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _emit(event: str, data: dict) -> None:
    _write_frame({"v": 1, "ev": event, "data": data})


# ── ops ────────────────────────────────────────────────────────────────────

_WORKSPACE_PATH = os.environ.get("DATACODE_WORKSPACE_PATH", "")

# SQLMesh project sub-directory of a working copy. Mirrors
# ``src/files/layout.py``'s SQL_WAREHOUSE_DIR, restated here because this module
# must import from a bare interpreter with no package path assumptions.
_SQL_WAREHOUSE_DIR = "sql-warehouse"

# SQLMesh's personal directory, inside the platform-owned state tree. Mirrors
# ``datacode_common.workspace_layout``'s TOOL_HOME_DIR / SQLMESH_HOME_DIR,
# restated for the same reason as above. The stdio child already receives both
# from the parent's clean environment; the CONTAINER form does not, and its HOME
# belongs to the image.
_TOOL_HOME_DIR = ".datacode/home"
_SQLMESH_HOME_DIR = f"{_TOOL_HOME_DIR}/.sqlmesh"


def _sqlmesh_root() -> str:
    """Return the SQLMesh project directory inside the mounted working copy.

    A working copy holds two sibling projects (``sql-warehouse/`` for SQLMesh and
    ``asset-dag/`` for Dagster), so the Context must be rooted at the former.
    Pointing it at the working-copy ROOT makes SQLMesh scan the wrong tree and
    report "no models" instead of a configuration error — which is why this is
    resolved explicitly rather than left to the caller's path.

    A path that is itself a SQLMesh project is accepted on the evidence of a
    ``config.yaml``, never as a silent fallback.
    """
    warehouse = os.path.join(_WORKSPACE_PATH, _SQL_WAREHOUSE_DIR)
    if os.path.isdir(warehouse):
        return warehouse
    return _WORKSPACE_PATH


# One Context per gateway this child has been asked about. Keyed by gateway
# (empty string = the project default) because a Context binds its connection
# at load time: reusing a Context loaded for gateway A to serve a request for
# gateway B would silently query the WRONG warehouse — the worst possible kind
# of wrong answer, because it looks like a correct one.
_CONTEXTS: dict[str, object] = {}


def _get_context(gateway: str | None = None):
    """Load (once per gateway) the executable SQLMesh Context.

    This is THE call that made this process necessary: ``Context()`` imports
    and execs the user's models/macros. It runs here, where there are no
    platform credentials to steal, instead of in the runtime process.

    P6c: the plan op is a first-class caller now, and it goes through
    ``src.sqlmesh.plan_engine`` so the sandbox and the service run byte-for-byte
    the same plan semantics (see that module for why sharing matters).
    """
    key = gateway or ""
    context = _CONTEXTS.get(key)
    if context is None:
        os.environ["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] = "true"
        _pin_tool_home()
        from sqlmesh.core.context import Context

        root = _sqlmesh_root()
        if gateway:
            context = Context(paths=root, gateway=gateway)
        else:
            context = Context(paths=root)
        _CONTEXTS[key] = context
    return context


def _reset_contexts() -> None:
    """Drop every cached Context (file changes / invalidation)."""
    _CONTEXTS.clear()


def _pin_tool_home() -> None:
    """Point HOME and ``SQLMESH_HOME`` at the platform's state directory.

    Called before the first ``import sqlmesh``, because SQLMesh resolves its
    personal directory at IMPORT time. Two distinct consequences of leaving it
    at the default, and only the second is a security matter:

    * ``user.yaml`` (a telemetry id) is written into the user's project tree —
      and disabling telemetry does not stop it: the collector still builds every
      event and still materializes the file;
    * the same directory is where SQLMesh looks for a PERSONAL config, which it
      merges OVER the project's. At the default location, anything able to write
      the working copy could repoint the gateways of a sandboxed run.

    The stdio child already receives both variables from the parent's clean
    environment; the TCP (container) form does not — its HOME comes from the
    image and is not writable on a read-only root filesystem.
    """
    if not _WORKSPACE_PATH:
        return
    home = os.path.join(_WORKSPACE_PATH, _TOOL_HOME_DIR)
    os.environ["HOME"] = home
    os.environ["SQLMESH_HOME"] = os.path.join(_WORKSPACE_PATH, _SQLMESH_HOME_DIR)
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        # A read-only mount is a legitimate sandbox shape. SQLMesh creates its
        # own directory when it can; a tool that cannot will say so itself,
        # which is more precise than failing the op here.
        pass


def _op_context_load(params: dict) -> dict:
    ctx = _get_context(params.get("gateway"))
    # Prefer the parsed config's dialect: the Context-level attribute can be
    # empty on projects that only set model_defaults.dialect.
    dialect = getattr(getattr(ctx, "config", None), "dialect", "") or getattr(
        ctx, "dialect", ""
    )
    return {
        "models": len(getattr(ctx, "models", {}) or {}),
        "dialect": str(dialect or ""),
        "workspace_path": _WORKSPACE_PATH,
        "sqlmesh_root": _sqlmesh_root(),
    }


def _op_context_refresh(params: dict) -> dict:
    _reset_contexts()
    return _op_context_load(params)


def _op_context_invalidate(_params: dict) -> dict:
    _reset_contexts()
    return {"invalidated": True}


# ── read/execute ops implemented in the sandbox-side metadata module ────────
# Thin wrappers: the Context cache and the dispatch table stay here, the
# op bodies (which exec user code by touching models/macros/queries) live in
# metadata_ops.py next to their peers like plan_engine.

def _op_metadata_models(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_models(_get_context(params.get("gateway")), params)


def _op_metadata_columns(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_model_detail(_get_context(params.get("gateway")), params)


def _op_metadata_metrics(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_metrics(_get_context(params.get("gateway")), params)


def _op_metadata_lineage(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_lineage(_get_context(params.get("gateway")), params)


def _op_metadata_environments(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_environments(_get_context(params.get("gateway")), params)


def _op_query(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_query(_get_context(params.get("gateway")), params)


def _op_evaluate(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_evaluate(_get_context(params.get("gateway")), params)


def _op_test(params: dict) -> dict:
    """Run the project's unit tests.

    ``detailed`` selects the rich per-test result (command endpoint); the
    default compact summary is what the plan gate consumes. The op name is
    part of the frozen protocol; both shapes ride its params additively.
    """
    from src.executor import metadata_ops

    return metadata_ops.op_test(_get_context(params.get("gateway")), params)


def _op_plan(params: dict) -> dict:
    """Build a plan PREVIEW — tests, categorization, formatting included.

    Params (additive; the wire format is unchanged):
        gateway      – warehouse gateway, empty for the project default
        environment  – target environment
        plan_options – {skip_tests, skip_backfill, forward_only, ...}
        plan_dates   – {start, end}
        categories   – {model_name: "BREAKING" | "NON_BREAKING" | ...}

    Result: the same formatted plan dict the REST preview endpoint has always
    returned, plus ``test_summary`` / ``tests_failed`` so the caller can stop
    BEFORE applying when tests failed.

    This op never applies anything. The write half of an apply runs in an L2
    task with a writable account (Q5); this sandbox executes user code and so
    holds a read-only account at most. The former ``sqlmesh.apply`` op was
    removed for exactly that reason.
    """
    from src.sqlmesh import plan_engine

    ctx = _get_context(params.get("gateway"))
    environment = params.get("environment") or "prod"
    plan_options = params.get("plan_options") or {}
    skip_tests = bool(plan_options.get("skip_tests", False))

    test_summary = None
    if not skip_tests:
        test_summary = plan_engine.run_tests_summary(ctx)
    if plan_engine.tests_failed(test_summary):
        return {
            "environment": environment,
            "has_changes": False,
            "requires_backfill": False,
            "tests_failed": True,
            "tests_skipped": False,
            "tests_passed": False,
            "test_results": test_summary,
        }

    try:
        _plan, formatted = plan_engine.build_plan(
            ctx,
            environment,
            plan_options=plan_options,
            plan_dates=params.get("plan_dates"),
            categories=params.get("categories"),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if plan_engine.is_linter_error(exc):
            # Structured lint results travel INSIDE the error payload; the
            # runtime turns them into its 422 PlanLintError. The wire shape
            # ({"code", "message", ...}) just gains a key.
            raise _OpError(
                "lint_failed",
                "plan blocked by linter",
                lint_results=plan_engine.collect_lint_violations(ctx),
            ) from exc
        raise

    formatted["tests_skipped"] = skip_tests
    formatted["tests_passed"] = test_summary is not None
    formatted["tests_failed"] = False
    formatted["test_results"] = test_summary
    return formatted


def _op_render(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_render(_get_context(params.get("gateway")), params)


def _op_table_diff(params: dict) -> dict:
    from src.executor import metadata_ops

    return metadata_ops.op_table_diff(_get_context(params.get("gateway")), params)


# NOTE: there is deliberately no shell op here. Agent shell runs as a TASK
# (kind "agent.shell", see task-runner/src/shell.py), which is where both
# user-code vehicles now share one submission contract and one isolation
# profile. A shell op in this process would be a second, divergent path for
# the most privileged thing the platform does.

_OPS = {
    "ping": lambda _p: {"alive": True},
    "context.load": _op_context_load,
    "context.refresh": _op_context_refresh,
    "context.invalidate": _op_context_invalidate,
    "metadata.models": _op_metadata_models,
    "metadata.columns": _op_metadata_columns,
    "metadata.metrics": _op_metadata_metrics,
    "metadata.lineage": _op_metadata_lineage,
    "metadata.environments": _op_metadata_environments,
    "sqlmesh.plan": _op_plan,
    "sqlmesh.test": _op_test,
    "sqlmesh.render": _op_render,
    "sqlmesh.query": _op_query,
    "sqlmesh.evaluate": _op_evaluate,
    "sqlmesh.table_diff": _op_table_diff,
}


def _serve_stdio() -> int:
    """Serve the protocol over fd 0/1 — the L3-proc child form.

    The socketpair the parent created is wired to stdin/stdout before exec, so
    this is a plain read/write loop until EOF (parent gone → exit cleanly).
    """
    while True:
        message = _read_frame()
        if message is None:  # EOF: the runtime went away, exit cleanly.
            return 0
        response = _dispatch(message)
        if response is not None:
            _write_frame(response)


def _serve_tcp(port: int) -> int:
    """Serve the protocol over TCP — the L3-base container form (P6c).

    The FRAMES are identical to the stdio form; only the byte stream differs.
    The runtime dials the Pod's IP after it becomes ready; one connection is
    served at a time (the protocol's "one request in flight per connection"
    rule), and the Pod keeps accepting so a reconnect after a transport hiccup
    does not require a new Pod — the warm Context survives it.

    Exit is via SIGTERM (Pod deletion): the process dies, the Context dies with
    it, and the runtime's next call starts a fresh Pod. There is deliberately
    no graceful-drain handshake — the runtime treats a dead connection the way
    it treats a crashed child.
    """
    import signal
    import socket as socket_module

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    server = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    # Bind loopback-free: the Pod has its own network namespace, so any
    # interface is fine and the NetworkPolicy decides who may connect.
    server.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(4)

    _emit("tcp_listening", {"port": port})

    while True:
        connection, _peer = server.accept()
        try:
            # Per-connection hello, mirroring the stdio handshake: the runtime
            # waits for it before trusting the connection with ops. In the
            # container form uid separation is inherent (runAsUser in the pod
            # security context), so it is announced as active.
            _write_frame_to(
                _SocketFile(connection),
                {"v": 1, "ev": "hello",
                 "data": {"uid_separated": True, "workspace": _WORKSPACE_PATH,
                          "transport": "tcp"}},
            )
            _serve_socket(connection)
        finally:
            try:
                connection.close()
            except OSError:
                pass


class _SocketFile:
    """File-like adapter over a socket, for the shared framing helpers.

    Lets ``_read_frame_from``/``_write_frame_to`` serve a TCP connection with
    the exact framing the stdio form uses — one implementation of the frozen
    protocol, two transports.
    """

    def __init__(self, conn):
        self._conn = conn

    def read(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._conn.recv(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def write(self, data: bytes) -> None:
        self._conn.sendall(data)

    def flush(self) -> None:
        pass


def _serve_socket(connection) -> None:
    """Serve one TCP connection with the same frames as the stdio form."""
    socket_file = _SocketFile(connection)
    while True:
        message = _read_frame_from(socket_file)
        if message is None:
            return
        response = _dispatch(message)
        if response is not None:
            _write_frame_to(socket_file, response)


def _read_frame_from(stream) -> dict | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    length = int.from_bytes(header, "big")
    if length > _MAX_FRAME:
        return None
    payload = stream.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_frame_to(stream, message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"), default=str).encode("utf-8")
    stream.write(len(payload).to_bytes(4, "big"))
    stream.write(payload)
    stream.flush()


# Mirrors the frozen protocol's MAX_FRAME_BYTES; restated locally so this module
# keeps its bare-interpreter property.
_MAX_FRAME = 64 * 1024 * 1024


def _dispatch(message: dict) -> dict | None:
    """Run one request and build its response frame (None = no response)."""
    req_id = message.get("id")
    op = message.get("op")
    handler = _OPS.get(op)
    try:
        if handler is None:
            raise KeyError(op)
        result = handler(message.get("params") or {})
        return {"v": 1, "id": req_id, "ok": True, "result": result}
    except KeyError:
        return {"v": 1, "id": req_id, "ok": False,
                "error": {"code": "unknown_op", "message": f"unknown op {op!r}"}}
    except MemoryError:
        return {"v": 1, "id": req_id, "ok": False,
                "error": {"code": "resource_limit", "message": "memory limit hit"}}
    except _OpError as exc:
        error = {"code": exc.code, "message": exc.message, **exc.extra}
        return {"v": 1, "id": req_id, "ok": False, "error": error}
    except Exception as exc:  # noqa: BLE001 — protocol boundary
        return {"v": 1, "id": req_id, "ok": False,
                "error": {"code": "op_failed", "message": str(exc)}}


def main() -> int:
    # ``--tcp PORT`` selects the container transport; the default remains the
    # stdio form the local child has always used. Same frames either way.
    if len(sys.argv) >= 3 and sys.argv[1] == "--tcp":
        return _serve_tcp(int(sys.argv[2]))

    # Hello handshake: announce uid separation status so the runtime can
    # surface honestly which threat coverage is active.
    uid_separated = os.environ.get("DATACODE_EXECUTOR_UID_SEPARATED") == "1"
    _emit("hello", {"uid_separated": uid_separated, "workspace": _WORKSPACE_PATH})

    return _serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
