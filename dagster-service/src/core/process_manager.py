"""Dagster subprocess lifecycle management.

Owns a single registry of managed subprocesses. Depending on the active mode:

- ``local``:  daemon + webserver are started here; Code Location gRPC servers
              are spawned via :class:`code_location_manager.CodeLocationManager`
              (which reuses this manager's ``spawn``/``stop`` primitives).
- ``remote``: no daemon/webserver locally (they live on the remote cluster);
              only Code Location gRPC servers run locally.

The Code Location gRPC server is ALWAYS local because it must load user Python
code from the workspace's ``asset-dag/`` directory.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import Settings, settings
from src.utils.errors import ProcessStartError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Well-known process names for the instance-level (non-workspace) processes.
DAEMON_PROCESS = "dagster-daemon"
WEBSERVER_PROCESS = "dagster-webserver"


@dataclass
class ManagedProcess:
    """A single supervised OS subprocess with graceful stop semantics."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    _process: Optional[subprocess.Popen] = None
    _started_at: Optional[float] = None

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

    def status(self) -> dict:
        alive = self.is_alive()
        return {
            "name": self.name,
            "pid": self._process.pid if self._process else None,
            "alive": alive,
            "uptime_seconds": (time.time() - self._started_at)
            if (alive and self._started_at)
            else None,
        }


class DagsterProcessManager:
    """Supervises all Dagster subprocesses for this service instance."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings
        self._processes: dict[str, ManagedProcess] = {}

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
    ) -> ManagedProcess:
        """Spawn (or replace) a named process and track it in the registry."""
        if name in self._processes and self._processes[name].is_alive():
            self.stop(name)
        proc = ManagedProcess(
            name=name,
            command=command,
            env={**self.build_dagster_env(), **(env or {})},
            cwd=cwd,
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
        """Start the mode-appropriate instance-level processes.

        Code Location gRPC servers are started lazily per-workspace by the
        CodeLocationManager, so here we only handle the daemon/webserver that
        exist in local mode. The webserver/daemon load the workspace.yaml that
        the CodeLocationManager keeps in sync as code locations come and go.
        """
        from src.core.code_location_manager import get_code_location_manager

        code_locations = get_code_location_manager()

        if self._settings.is_remote:
            # Remote mode: DagsterInstance (daemon + webserver + DB) is remote.
            # Nothing to start here; code locations are started on demand and
            # registered to the remote instance by the CodeLocationManager.
            logger.info("dagster_process_manager_remote_mode", graphql=self._settings.active_graphql_url)
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
        logger.info(
            "dagster_process_manager_local_mode_started",
            daemon=self._settings.dagster_daemon_enabled,
            webserver=self._settings.dagster_webserver_enabled,
        )

    async def stop_all(self) -> None:
        """Stop every managed process (code locations included)."""
        for name in list(self._processes.keys()):
            self.stop(name)
        logger.info("dagster_process_manager_stopped")

    def health_check(self) -> dict:
        """Return mode + per-process status for the health endpoint."""
        return {
            "mode": self._settings.dagster_mode,
            "processes": [p.status() for p in self._processes.values()],
        }

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
