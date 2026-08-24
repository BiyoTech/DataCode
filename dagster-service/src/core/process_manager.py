"""Dagster subprocess lifecycle management and supervision.

Owns a single registry of managed subprocesses. Depending on the active mode:

- ``local``:  daemon + webserver are started here; Code Location gRPC servers
              are spawned via :class:`code_location_manager.CodeLocationManager`
              (which reuses this manager's ``spawn``/``stop`` primitives).
- ``remote``: no daemon/webserver locally (they live on the remote cluster);
              only Code Location gRPC servers run locally.

The Code Location gRPC server is ALWAYS local because it must load user Python
code from the working copy's ``asset-dag/`` directory.

Supervision
-----------
A background task periodically re-checks every managed process and respawns the
ones that died. Without it a crashed code location stays dead until somebody
happens to trigger an ensure, and its deployment silently disappears from the
Dagster instance — the failure surfaces as "my assets are gone", far from its
cause.

Restarts are BOUNDED: a process that keeps dying (invalid user code, a port
clash) is retried a few times inside a window and then left alone, with the
give-up recorded in its status. Restarting forever would turn a broken working
copy into a permanent spawn loop and hide the real error.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import Settings, settings
from src.utils.errors import ProcessStartError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Well-known process names for the instance-level (non-workspace) processes.
DAEMON_PROCESS = "dagster-daemon"
WEBSERVER_PROCESS = "dagster-webserver"

# Callback awaited after a supervisor-driven restart, so the owner can re-attach
# the process to whatever depends on it (a code location to the webserver).
RestartHook = Callable[[], Awaitable[None]]


@dataclass
class ManagedProcess:
    """A single supervised OS subprocess with graceful stop semantics."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    # Whether the supervisor may respawn this process when it dies.
    autorestart: bool = True
    on_restart: Optional[RestartHook] = None
    _process: Optional[subprocess.Popen] = None
    _started_at: Optional[float] = None
    # Timestamps of supervisor-driven restarts, trimmed to the current window.
    _restarts: list[float] = field(default_factory=list)
    # Set once the restart budget is exhausted: the process is left dead and the
    # condition is reported instead of retried forever.
    abandoned: bool = False

    def start(self) -> None:
        """Spawn the subprocess. Inherits the parent env plus ``self.env``."""
        merged_env = {**os.environ, **self.env}
        logger.info("dagster_process_starting", name=self.name, command=" ".join(self.command))
        try:
            # start_new_session=True puts the child in its own process group so
            # we can signal the whole group and avoid orphaned grandchildren.
            self._process = subprocess.Popen(  # noqa: S603 - command is built from config, not user input
                self.command,
                env=merged_env,
                cwd=self.cwd,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            raise ProcessStartError(self.name, detail=str(e)) from e
        self._started_at = time.time()

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def exit_code(self) -> Optional[int]:
        """Exit status of a finished process, or None while it runs."""
        return self._process.poll() if self._process else None

    def stop(self, timeout: int) -> None:
        """Gracefully terminate: SIGTERM the group, wait, then SIGKILL."""
        if self._process is None or self._process.poll() is not None:
            return
        pid = self._process.pid
        logger.info("dagster_process_stopping", name=self.name, pid=pid)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("dagster_process_kill", name=self.name, pid=pid)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def restart_allowed(self, *, max_restarts: int, window_seconds: int) -> bool:
        """Whether the restart budget still allows a respawn.

        A sliding window rather than a total count: a process that has been up
        for days should get its retries back, while one crash-looping right now
        must be given up on.
        """
        now = time.time()
        self._restarts = [at for at in self._restarts if now - at < window_seconds]
        return len(self._restarts) < max_restarts

    def note_restart(self) -> None:
        self._restarts.append(time.time())

    @property
    def restart_count(self) -> int:
        return len(self._restarts)

    def status(self) -> dict:
        alive = self.is_alive()
        return {
            "name": self.name,
            "pid": self._process.pid if self._process else None,
            "alive": alive,
            "uptime_seconds": (time.time() - self._started_at)
            if (alive and self._started_at)
            else None,
            "restart_count": self.restart_count,
            "abandoned": self.abandoned,
        }


class DagsterProcessManager:
    """Supervises all Dagster subprocesses for this service instance."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings
        self._processes: dict[str, ManagedProcess] = {}
        self._supervisor: Optional[asyncio.Task] = None

    # --- Generic process primitives (reused by CodeLocationManager) ----------

    def build_dagster_env(self) -> dict[str, str]:
        """Environment shared by all Dagster subprocesses.

        ``DAGSTER_HOME`` points the local DagsterInstance (and its SQLite
        storage) at a stable directory. In remote mode the code location gRPC
        server does not need a local instance, but setting a home is harmless
        and keeps behavior uniform.
        """
        return {
            "DAGSTER_HOME": self._dagster_home(),
            "DAGSTER_TELEMETRY_ENABLED": "false",
        }

    def spawn(
        self, name: str, command: list[str], *, cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        autorestart: bool = True,
        on_restart: Optional[RestartHook] = None,
    ) -> ManagedProcess:
        """Spawn (or replace) a named process and track it in the registry.

        An explicit spawn also clears the restart budget: the caller is acting on
        new information (fresh code, a fixed config), so the previous crash loop
        must not keep the process abandoned.
        """
        if name in self._processes and self._processes[name].is_alive():
            self.stop(name)
        proc = ManagedProcess(
            name=name,
            command=command,
            env={**self.build_dagster_env(), **(env or {})},
            cwd=cwd,
            autorestart=autorestart,
            on_restart=on_restart,
        )
        proc.start()
        self._processes[name] = proc
        return proc

    def stop(self, name: str) -> None:
        proc = self._processes.get(name)
        if proc is not None:
            proc.stop(self._settings.process_stop_timeout)
            self._processes.pop(name, None)

    def get(self, name: str) -> Optional[ManagedProcess]:
        return self._processes.get(name)

    # --- Instance lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Start the mode-appropriate instance-level processes and the supervisor.

        Code Location gRPC servers are started lazily per-deployment by the
        CodeLocationManager, so here we only handle the daemon/webserver that
        exist in local mode. The webserver/daemon load the workspace.yaml that
        the CodeLocationManager keeps in sync as deployments come and go.
        """
        from src.core.code_location_manager import get_code_location_manager

        code_locations = get_code_location_manager()

        if self._settings.is_remote:
            # Remote mode: DagsterInstance (daemon + webserver + DB) is remote.
            # Nothing to start here; code locations are started on demand and
            # registered to the remote instance by the CodeLocationManager.
            logger.info(
                "dagster_process_manager_remote_mode",
                graphql=self._settings.active_graphql_url,
            )
            self._start_supervisor()
            return

        # Local mode: ensure home + an initial (possibly empty) workspace.yaml,
        # then start daemon + webserver pointing at it.
        self._prepare_dagster_home()
        workspace_yaml = code_locations.ensure_workspace_yaml()

        if self._settings.dagster_daemon_enabled:
            self.spawn(
                DAEMON_PROCESS,
                ["dagster-daemon", "run", "-w", str(workspace_yaml)],
            )
        if self._settings.dagster_webserver_enabled:
            self.spawn(
                WEBSERVER_PROCESS,
                [
                    "dagster-webserver",
                    "-h", self._settings.dagster_webserver_host,
                    "-p", str(self._settings.dagster_webserver_port),
                    "-w", str(workspace_yaml),
                ],
            )
        self._start_supervisor()
        logger.info(
            "dagster_process_manager_local_mode_started",
            daemon=self._settings.dagster_daemon_enabled,
            webserver=self._settings.dagster_webserver_enabled,
        )

    async def stop_all(self) -> None:
        """Stop the supervisor and every managed process (code locations included)."""
        await self._stop_supervisor()
        for name in list(self._processes.keys()):
            self.stop(name)
        logger.info("dagster_process_manager_stopped")

    def health_check(self) -> dict:
        """Return mode + per-process status for the health endpoint."""
        return {
            "mode": self._settings.dagster_mode,
            "supervised": self._supervisor is not None and not self._supervisor.done(),
            "processes": [p.status() for p in self._processes.values()],
        }

    # --- Supervision ---------------------------------------------------------

    def _start_supervisor(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._supervisor = asyncio.create_task(self._supervise())
        logger.info(
            "dagster_process_supervisor_started",
            interval_seconds=self._settings.process_health_interval,
            max_restarts=self._settings.process_max_restarts,
            window_seconds=self._settings.process_restart_window,
        )

    async def _stop_supervisor(self) -> None:
        task, self._supervisor = self._supervisor, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _supervise(self) -> None:
        """Periodically respawn processes that died, within their budget."""
        interval = max(1, self._settings.process_health_interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self._respawn_dead()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the supervisor must never die
                logger.error("dagster_process_supervisor_error", error=str(e), exc_info=True)

    async def _respawn_dead(self) -> None:
        for proc in list(self._processes.values()):
            if proc.is_alive() or proc.abandoned or not proc.autorestart:
                continue
            if not proc.restart_allowed(
                max_restarts=self._settings.process_max_restarts,
                window_seconds=self._settings.process_restart_window,
            ):
                proc.abandoned = True
                logger.error(
                    "dagster_process_abandoned",
                    name=proc.name,
                    exit_code=proc.exit_code(),
                    restart_count=proc.restart_count,
                    window_seconds=self._settings.process_restart_window,
                )
                continue

            logger.warning(
                "dagster_process_died_respawning",
                name=proc.name,
                exit_code=proc.exit_code(),
                restart_count=proc.restart_count,
            )
            proc.note_restart()
            try:
                proc.start()
            except ProcessStartError as e:
                logger.error("dagster_process_respawn_failed", name=proc.name, error=str(e))
                continue

            if proc.on_restart is None:
                continue
            # The owner re-attaches the process to its dependents (a code
            # location to the Dagster instance). Failures are reported, never
            # fatal: the process itself is already back up.
            try:
                await proc.on_restart()
            except Exception as e:  # noqa: BLE001 - hook is best-effort
                logger.warning(
                    "dagster_process_restart_hook_failed", name=proc.name, error=str(e)
                )

    # --- Helpers -------------------------------------------------------------

    def _dagster_home(self) -> str:
        return str(Path(self._settings.dagster_home).expanduser())

    def _prepare_dagster_home(self) -> None:
        """Create DAGSTER_HOME. Dagster auto-provisions SQLite storage there
        when no dagster.yaml is present, which is exactly the zero-dependency
        local default we want."""
        home = Path(self._dagster_home())
        home.mkdir(parents=True, exist_ok=True)
        logger.info("dagster_home_ready", path=str(home))


_manager: Optional[DagsterProcessManager] = None


def get_process_manager() -> DagsterProcessManager:
    """Return the process-wide manager singleton."""
    global _manager
    if _manager is None:
        _manager = DagsterProcessManager()
    return _manager
