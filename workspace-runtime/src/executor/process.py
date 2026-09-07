"""L3-proc executor — the parent (runtime) side (design 04 §2.3, P3e).

One PERSISTENT child process per working copy holds the EXECUTABLE SQLMesh
Context. Constructing that Context execs the user's Python models and macros,
which must never happen in this service's process (threat T2: the platform
holds DB/LLM/encryption credentials in its environment).

Hardening applied to every child (the L3_proc profile, conf/isolation-profiles.yaml):
  * environment CLEARED, then re-injected from a strict allowlist —
    os.environ can no longer surface any platform secret;
  * rlimits lowered (RLIMIT_AS / CPU / NPROC / FSIZE / NOFILE) — allowed
    unprivileged, they bound memory, CPU time, fork bombs and disk fills;
  * cwd pinned to the working copy;
  * git hooks disabled via core.hooksPath in any git invocation;
  * uid separation is applied when the process holds CAP_SETUID (production
    containers) and skipped with a loud warning otherwise — env clearing and
    rlimits are the unprivileged core of the profile and always active.

The wire protocol is the FROZEN contract in datacode_common.executor_protocol
(P0). P6 replaces this child process with an L3-base container speaking the
same protocol, without touching the caller.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from datacode_common.executor_protocol import (
    ERR_CRASHED,
    OP_CONTEXT_REFRESH,
    OP_PING,
    ProtocolError,
    encode_message,
    make_request,
    read_message,
)

from src.config import settings
from src.isolation.hardening import (
    ResourceLimits,
    apply_rlimits,
    build_clean_env,
    try_uid_separation,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# One in-flight request per child connection at a time; a second concurrent
# op against the same working copy gets its own connection (children are
# single-threaded, so this only parallelizes cheap reads, which is fine).
_REQUEST_TIMEOUT = float(os.getenv("WORKSPACE_RUNTIME_EXECUTOR_TIMEOUT", "900"))


@dataclass
class _ChildHandle:
    """A live executor sandbox and its parent-side connection.

    ``sock`` is the socketpair end in process mode; in container mode the
    writer alone owns the TCP connection and ``pod_name`` names the Pod to
    delete on teardown.
    """

    workspace_path: str
    gateway: str
    sock: Optional[socket.socket]
    reader: asyncio.StreamReader = field(init=False)
    writer: asyncio.StreamWriter = field(init=False)
    pod_name: str = ""
    last_used: float = field(default_factory=time.time)
    restarts: list[float] = field(default_factory=list)


class ExecutorError(RuntimeError):
    """An executor op failed (protocol error, crash or op-level error).

    ``payload`` carries the op's structured error dict verbatim (code, message,
    plus any op-specific keys such as lint results), so a caller that needs more
    than the code does not have to re-run the failure to find out what it said.
    """

    def __init__(self, code: str, message: str, payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {"code": code, "message": message}

    @property
    def detail(self) -> dict:
        """The structured error payload; ``payload.get("lint_results")`` etc."""
        return self.payload


def _child_env(workspace_path: str, environment: str) -> dict[str, str]:
    """Build the child's environment per the L3_proc allowlist.

    Thin wrapper over the shared primitive: the hardening itself lives in
    ``src.isolation.hardening`` because the one-shot task backend applies the
    identical policy, and a second copy that forgot one variable would be
    indistinguishable from no hardening at all.

    The only executor-specific part is PYTHONPATH: this child re-execs
    ``src.executor.server``, so it needs this package's root and the shared
    library. Those are filesystem locations, not secrets.
    """
    # .../workspace-runtime/src/executor/process.py -> parents[2] is the package
    # root containing src/.
    runtime_root = Path(__file__).resolve().parents[2]
    shared_root = Path(__file__).resolve().parents[3] / "shared" / "python"
    return build_clean_env(
        workspace_path=workspace_path,
        environment=environment,
        python_path=(str(runtime_root), str(shared_root), os.getenv("PYTHONPATH", "")),
    )


def _executor_limits() -> ResourceLimits:
    """Resource limits for an executor child.

    Sourced from ``settings`` rather than from the profile because the executor
    predates the profile loader (P3e vs P6a) and its values are per-deployment
    tunables today. They mirror the ``L3_proc`` rlimits block; converging the two
    onto the profile is a P6c follow-up recorded in the roadmap.
    """
    return ResourceLimits(
        address_space_bytes=settings.executor_address_space_bytes,
        cpu_seconds=settings.executor_cpu_seconds,
        max_processes=settings.executor_max_processes,
        max_file_size_bytes=settings.executor_max_file_size_bytes,
        max_open_files=settings.executor_max_open_files,
    )


def _spawn_child(  # noqa: PLR0913
    workspace_path: str,
    environment: str,
    slot: int,
    sock: socket.socket,
) -> int:
    """Fork the executor child. Runs IN THE CHILD after fork; never returns
    there (execs the server loop and exits with its status)."""

    # MUST run before any logging/await machinery: the child must not carry
    # a copy of the parent's event loop state.
    if os.fork() != 0:
        return os.getpid()  # parent branch — signal only; caller ignores

    # ── child from here on ─────────────────────────────────────────────
    os.setsid()
    try:
        apply_rlimits(_executor_limits())
        os.chdir(workspace_path)
        uid_applied = try_uid_separation(settings.executor_user_base + slot)
        env = _child_env(workspace_path, environment)
        env["DATACODE_EXECUTOR_UID_SEPARATED"] = "1" if uid_applied else "0"

        # Re-exec a clean interpreter running ONLY the server module, with
        # the socketpair as fd 0/1. A fresh interpreter (not forked state)
        # guarantees no parent memory — including secrets — survives.
        server_module = "src.executor.server"
        os.dup2(sock.fileno(), 0)
        os.dup2(sock.fileno(), 1)
        sock.close()
        os.execve(
            sys.executable,
            [sys.executable, "-m", server_module],
            env,
        )
    except Exception as exc:  # noqa: BLE001 — child cannot raise to parent
        os.write(2, f"executor_child_spawn_failed: {exc}\n".encode())
        os._exit(127)
    raise AssertionError("execve does not return")


class ExecutorManager:
    """Owns one persistent executor child per (working copy, gateway).

    The child holds the hot SQLMesh Context, so isolation costs one socket
    round trip instead of a Context rebuild. Children are restarted with a
    bounded budget (a crash-looping executor surfaces via /health instead of
    spinning forever) and stopped after an idle window.

    Keying includes the GATEWAY since P6c, when plan preview became an executor
    caller: a Context binds its connection at load time, so a child serving
    gateway B with a Context loaded for gateway A would query the wrong
    warehouse. One child per (copy, gateway) makes that impossible by
    construction.

    Context freshness: the sandbox cannot see file writes the runtime made, so
    the runtime must tell it. ``mark_context_dirty`` (called wherever the
    context pool is marked dirty) causes the NEXT op to reload the Context
    first; ``invalidate`` (git switches, post-apply) stops the children
    outright, trading the warm Context for correctness — the same trade the
    pool makes at the same call sites.
    """

    def __init__(self) -> None:
        self._children: dict[tuple[str, str], _ChildHandle] = {}
        self._dirty: set[str] = set()
        self._next_id = 0
        self._slot = 0
        self._restart_log: list[float] = []
        # Container-mode backend, built on first use so a process-mode
        # deployment never imports the Kubernetes client.
        self._sandbox_backend = None

    # ── public API ─────────────────────────────────────────────────────

    async def call(
        self,
        workspace_path: str,
        environment: str,
        op: str,
        params: Optional[dict[str, Any]] = None,
        *,
        gateway: str = "",
    ) -> dict[str, Any]:
        """Invoke ``op`` on the working copy's executor, starting it if needed.

        Raises ExecutorError on protocol failures, crashes or op errors.
        """
        key = (workspace_path, gateway or "")
        handle = self._children.get(key)
        if handle is None:
            handle = await self._start(workspace_path, environment, gateway)
            self._children[key] = handle
            self._dirty.discard(workspace_path)
        handle.last_used = time.time()

        # A fresh child reloads its Context on first use, so only an already
        # warm child needs the explicit refresh.
        if workspace_path in self._dirty:
            await self._exchange(handle, OP_CONTEXT_REFRESH, {"gateway": gateway or None})
            self._dirty.discard(workspace_path)
            handle.last_used = time.time()

        response = await self._exchange(handle, op, params)
        if not response.get("ok"):
            err = response.get("error") or {}
            raise ExecutorError(
                err.get("code", "unknown"),
                err.get("message", "op failed"),
                payload=err,
            )
        return response.get("result") or {}

    async def _exchange(
        self, handle: _ChildHandle, op: str, params: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Send one request and read its response on the child's connection."""
        self._next_id += 1
        request = make_request(self._next_id, op, params)
        try:
            handle.writer.write(encode_message(request))
            await handle.writer.drain()
            response = await asyncio.wait_for(read_message(handle.reader), _REQUEST_TIMEOUT)
        except (ProtocolError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
            # Child is dead or misbehaving: tear it down, surface a crash
            # error, and let the next call restart it (within the budget).
            self._stop(handle)
            raise ExecutorError(ERR_CRASHED, f"executor call failed: {exc}") from exc

        if not response:
            self._stop(handle)
            raise ExecutorError(ERR_CRASHED, "executor closed the connection")
        return response

    async def ping(self, workspace_path: str, environment: str) -> bool:
        try:
            await self.call(workspace_path, environment, OP_PING)
            return True
        except ExecutorError:
            return False

    def mark_context_dirty(self, workspace_path: str) -> None:
        """Note that files changed under the working copy.

        The next op against ANY gateway of this working copy reloads the
        sandbox's Context first, mirroring the pool's refresh_if_dirty.
        """
        self._dirty.add(workspace_path)

    def invalidate(self, workspace_path: str) -> None:
        """Stop every child for a working copy (git switch, post-apply, ...).

        Not a mere refresh: after a branch switch the previously loaded Context
        describes a tree that no longer exists, and the per-gateway children may
        be several.
        """
        for key in [k for k in self._children if k[0] == workspace_path]:
            self._retire(self._children.pop(key), failure=False)

    def invalidate_under_path(self, root_path: str) -> int:
        """Stop every child whose working copy lives under ``root_path``.

        Workspace teardown path: one workspace holds one working copy per
        member and environment, each with its own executor children. Returns
        the number of retired children.
        """
        prefix = root_path.rstrip("/") + "/"
        keys = [k for k in self._children if k[0].startswith(prefix)]
        for key in keys:
            self._retire(self._children.pop(key), failure=False)
        if keys:
            logger.info(
                "executors_invalidated_under_path", root_path=root_path, count=len(keys)
            )
        return len(keys)

    def active_workspaces(self) -> list[str]:
        return sorted({key[0] for key in self._children})

    async def shutdown(self) -> None:
        """Stop every child WITHOUT consuming the restart budget — this is a
        clean lifecycle exit (drain / service stop), not a crash."""
        pending: list = []
        for key in list(self._children):
            handle = self._children.pop(key)
            self._close_handle(handle)
            if handle.pod_name and self._sandbox_backend is not None:
                pending.append(self._sandbox_backend.stop(handle.pod_name))
            logger.info("executor_stopped_clean", workspace_path=key[0], gateway=key[1])
        if pending:
            # Awaited (not fire-and-forget) so a draining service does not
            # abandon live Pods on the way out.
            await asyncio.gather(*pending, return_exceptions=True)

    # ── internals ──────────────────────────────────────────────────────

    async def _start(
        self, workspace_path: str, environment: str, gateway: str = ""
    ) -> _ChildHandle:
        Path(workspace_path).resolve()  # validates existence cheaply

        # Restart budget: abandon after executor_max_restarts inside the
        # window, mirroring the subprocess supervisor semantics.
        now = time.time()
        window_start = now - settings.executor_restart_window_seconds
        prior = [t for t in self._restart_log if t > window_start]
        if len(prior) > settings.executor_max_restarts:
            raise ExecutorError(
                ERR_CRASHED,
                "executor exceeded restart budget; see /health (abandoned, not respawned)",
            )

        # State-schema reconciliation BEFORE the sandbox loads the project.
        # This was the ContextPool's job; since the read-side services moved
        # into the sandbox (P6 close-out) the pool no longer exists and the
        # sandbox's launch point is the one place every Context load funnels
        # through. A mismatch raises StateSchemaMismatchError (a ServiceError
        # -> 409) and propagates to the caller untouched — deliberately NOT
        # wrapped into an ExecutorError, which would flatten it into a 500.
        self._reconcile_state_schema(workspace_path)

        mode = (settings.executor_mode or "process").strip().lower()
        if mode == "container":
            handle = await self._start_container(workspace_path, environment, gateway)
        else:
            handle = await self._start_process(workspace_path, environment, gateway)

        # Handshake: the sandbox announces itself (and whether uid separation
        # is active) before we trust it with ops. Identical for both vehicles —
        # the container form emits the same hello frame on each TCP connection.
        hello = await asyncio.wait_for(read_message(handle.reader), 10.0)
        if hello.get("ev") != "hello":
            raise ExecutorError(ERR_CRASHED, f"bad executor handshake: {hello!r}")
        logger.info(
            "executor_started",
            workspace_path=workspace_path,
            gateway=gateway or None,
            environment=environment,
            mode=mode,
            uid_separated=hello.get("data", {}).get("uid_separated"),
        )
        return handle

    async def _start_process(
        self, workspace_path: str, environment: str, gateway: str
    ) -> _ChildHandle:
        """The L3-proc vehicle: fork a hardened child over a socketpair."""
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._slot = (self._slot + 1) % 1000
            _spawn_child(workspace_path, environment, self._slot, child_sock)
        finally:
            child_sock.close()

        parent_sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=parent_sock)
        handle = _ChildHandle(
            workspace_path=workspace_path, gateway=gateway or "", sock=parent_sock
        )
        handle.reader = reader
        handle.writer = writer
        return handle

    async def _start_container(
        self, workspace_path: str, environment: str, gateway: str
    ) -> _ChildHandle:
        """The L3-base vehicle (P6c): a Pod speaking the same protocol over TCP.

        The sock field stays None — the writer owns the TCP connection — and the
        Pod's name rides on the handle so teardown can delete exactly what
        start created.
        """

        backend = self._container_backend()
        pod_name, host, port = await backend.start(workspace_path, environment, gateway)
        reader, writer = await asyncio.open_connection(host, port)
        handle = _ChildHandle(
            workspace_path=workspace_path,
            gateway=gateway or "",
            sock=None,  # type: ignore[arg-type]
        )
        handle.reader = reader
        handle.writer = writer
        handle.pod_name = pod_name
        return handle

    def _container_backend(self):
        """Lazily built so a process-mode deployment never imports the client."""
        if self._sandbox_backend is None:
            from src.executor.container import WarmSandboxBackend

            self._sandbox_backend = WarmSandboxBackend()
        return self._sandbox_backend

    @staticmethod
    def _reconcile_state_schema(workspace_path: str) -> None:
        """Ensure the project's ``state_schema`` is the managed one.

        Idempotent and cheap (one YAML parse); a gateway without a value gets
        the managed one written, a gateway with a DIFFERENT value aborts the
        sandbox launch with the 409 the repair flow expects. When the file was
        rewritten, the project's local ``.cache`` is dropped — it may hold
        snapshots read from a different state schema.
        """
        import shutil

        from src.files.layout import sqlmesh_root
        from src.sqlmesh.config_ops import reconcile_state_schema
        from src.workspace.identity import resolve_workspace_identity

        project_root = sqlmesh_root(workspace_path)
        identity = resolve_workspace_identity(workspace_path)
        reconciliation = reconcile_state_schema(project_root, identity.workspace_id)
        if reconciliation.changed:
            cache_dir = Path(project_root) / ".cache"
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir, ignore_errors=True)
            logger.info(
                "executor_state_schema_reconciled",
                workspace_path=workspace_path,
                gateways=list(reconciliation.generated_for),
            )

    def _stop(self, handle: _ChildHandle) -> None:
        """Teardown after a FAILURE: closes and consumes restart budget."""
        self._retire(handle, failure=True)

    def _retire(self, handle: _ChildHandle, *, failure: bool) -> None:
        """Close a sandbox's connection, stopping its Pod when it has one.

        Container mode cannot rely on socket EOF — a Pod does not die with the
        connection — so the Pod's deletion is scheduled explicitly. Teardown is
        fire-and-forget on purpose: the failure path must not hang on a slow
        API server, and an orphaned Pod is observable (it carries the
        warm-sandbox label) rather than silent.
        """
        self._close_handle(handle)
        if failure:
            self._restart_log.append(time.time())
            logger.warning(
                "executor_stopped_after_failure",
                workspace_path=handle.workspace_path,
                gateway=handle.gateway,
            )
        if handle.pod_name and self._sandbox_backend is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self._sandbox_backend.stop(handle.pod_name)
                )
            except RuntimeError:  # no loop (interpreter teardown)
                pass

    @staticmethod
    def _close_handle(handle: _ChildHandle) -> None:
        try:
            handle.writer.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        if handle.sock is not None:
            try:
                handle.sock.close()
            except Exception:  # noqa: BLE001
                pass


_manager: Optional[ExecutorManager] = None


def get_executor_manager() -> ExecutorManager:
    """Process-wide executor manager singleton (empty until first use)."""
    global _manager
    if _manager is None:
        _manager = ExecutorManager()
    return _manager
