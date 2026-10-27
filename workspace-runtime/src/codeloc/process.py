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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

import yaml

from src.codeloc.errors import ProcessStartError
from src.config import Settings, settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Well-known process names for the instance-level (non-workspace) processes.
#
# The webserver is a SEPARATE process from the daemon on purpose: the two are
# switched independently (``dagster.webserver_enabled`` /
# ``dagster.daemon_enabled``), and a UI restart must not disturb schedule
# execution. Since Dagster 1.13 the webserver lives in its own distribution
# (``dagster-webserver``) rather than in ``dagster``, so the binary exists only
# when that package is installed — see the note in pyproject.toml.
DAEMON_PROCESS = "dagster-daemon"
WEBSERVER_PROCESS = "dagster-webserver"

# Upper bound for the pre-flight instance migration (see
# ``DagsterProcessManager._migrate_instance``). A cold run — which creates every
# storage table — measures around one second, and an already-migrated home is
# barely faster, so this is a runaway guard rather than a budget: it must never
# be the reason boot hangs.
INSTANCE_MIGRATE_TIMEOUT_SECONDS = 300

# ``dagster.yaml`` keys that point instance storage at something this replica
# does not own. Any of them present makes the pre-flight migration unsafe.
EXTERNAL_STORAGE_KEYS = (
    "storage",
    "run_storage",
    "event_log_storage",
    "schedule_storage",
)


def dagster_search_path() -> str:
    """``PATH`` for Dagster subprocesses: this interpreter's ``bin/`` first.

    Every Dagster command is spawned by its bare name (``dagster-daemon``,
    ``dagster-webserver``, ``dagster api grpc``), which POSIX resolves against
    the PATH of the environment handed to ``execvpe`` — so the console scripts
    must be reachable there or the spawn fails with ENOENT before Dagster ever
    runs.

    Inheriting PATH alone is not enough. The dev flow starts this service as
    ``<svc>/.venv/bin/python -m uvicorn`` WITHOUT activating the virtualenv:
    ``sys.executable`` is the venv interpreter, but ``.venv/bin`` — the only
    directory holding its ``dagster*`` scripts — is absent from PATH. The
    daemon, the webserver and every code location then fail to start, and the
    asset DAG surfaces it far from here as "Dagster instance is unavailable at
    http://localhost:3001/graphql".

    Prepended, not appended: a code location must load user code with the SAME
    interpreter that carries this service's SQLMesh/Dagster versions, so our
    own scripts must win over any global install. The inherited PATH follows,
    which keeps ``git`` and friends resolvable for Dagster's own subprocesses.

    NOT resolved through symlinks: a virtualenv's ``python`` is a link to the
    base interpreter, so resolving it lands in the BASE install's ``bin/``
    (e.g. Homebrew's), which holds no ``dagster`` — the same trap documented in
    ``src/tasks/backends/local.py``. In a container image the interpreter and
    the scripts share ``/usr/local/bin``, so this entry is a harmless no-op.
    """
    interpreter_bin = str(Path(sys.executable).absolute().parent)
    inherited = os.environ.get("PATH") or os.defpath
    segments = [interpreter_bin]
    segments.extend(p for p in inherited.split(os.pathsep) if p and p != interpreter_bin)
    return os.pathsep.join(segments)

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
    # Where stdout and stderr go. Unset means "inherit this service's", which is
    # right for the Dagster daemon and webserver (their output belongs in the
    # platform's own log stream). A code location is different: what it prints is
    # the USER's code, and the traceback of a failed import is the only
    # explanation a failed reload has. Redirecting it to a file inside the
    # working copy puts that explanation where both the UI and the agent can read
    # it, instead of interleaved into the service's stdout.
    log_path: Optional[str] = None
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
        sink = self._open_log_sink()
        try:
            # start_new_session=True puts the child in its own process group so
            # we can signal the whole group and avoid orphaned grandchildren.
            self._process = subprocess.Popen(  # noqa: S603 - command is built from config, not user input
                self.command,
                env=merged_env,
                cwd=self.cwd,
                start_new_session=True,
                stdout=sink,
                stderr=subprocess.STDOUT if sink is not None else None,
            )
        except FileNotFoundError as e:
            # The executable itself is missing. Named separately from other
            # OSErrors because the cause is almost always resolution, not the
            # binary: report WHAT was looked for and WHERE, so the failure is
            # actionable instead of just "could not start".
            raise ProcessStartError(
                self.name,
                detail=(
                    f"executable not found: {self.command[0]} "
                    f"(searched PATH={merged_env.get('PATH', '')}) — "
                    f"{e}"
                ),
            ) from e
        except (OSError, ValueError) as e:
            raise ProcessStartError(self.name, detail=str(e)) from e
        finally:
            if sink is not None:
                # The child holds its own duplicate of the descriptor; keeping
                # this one open would leak one per restart.
                sink.close()
        self._started_at = time.time()

    def _open_log_sink(self):
        """Open :attr:`log_path` for the child, or return ``None``.

        Truncated per start: this file is the transcript of THE CURRENT process,
        which is what makes "the reload failed, here is why" unambiguous. History
        would mean parsing out where the last attempt began.

        A path that cannot be opened degrades to inherited output rather than
        failing the start — losing the transcript costs diagnostics, while
        refusing to start costs the user their code location.
        """
        if not self.log_path:
            return None
        try:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            return open(self.log_path, "wb")  # noqa: SIM115 — handed to Popen
        except OSError as exc:
            logger.warning(
                "dagster_process_log_unavailable",
                name=self.name,
                path=self.log_path,
                error=str(exc),
            )
            return None

    def output_tail(self, max_bytes: int = 8192) -> str:
        """End of this process's transcript, or ``""`` when there is none.

        The tail rather than the head: a Python traceback ends with the line that
        actually failed, and an import error in a user's asset is the last thing
        the process writes before exiting.
        """
        if not self.log_path:
            return ""
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

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

        ``PATH`` is set here — the one place every Dagster subprocess passes
        through, instance processes and code locations alike — so that the
        ``dagster*`` console scripts resolve from this service's own
        interpreter; see :func:`dagster_search_path`.
        """
        return {
            "DAGSTER_HOME": self._dagster_home(),
            "DAGSTER_TELEMETRY_ENABLED": "false",
            "PATH": dagster_search_path(),
        }

    def spawn(
        self, name: str, command: list[str], *, cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        autorestart: bool = True,
        on_restart: Optional[RestartHook] = None,
        log_path: Optional[str] = None,
    ) -> ManagedProcess:
        """Spawn (or replace) a named process and track it in the registry.

        An explicit spawn also clears the restart budget: the caller is acting on
        new information (fresh code, a fixed config), so the previous crash loop
        must not keep the process abandoned.

        Args:
            log_path: Redirect stdout/stderr to this file. See
                :attr:`ManagedProcess.log_path`.
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
            log_path=log_path,
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

        The instance storage is provisioned FIRST, in every mode, because every
        Dagster subprocess opens it — see :meth:`_migrate_instance`.
        """
        from src.codeloc.manager import get_code_location_manager

        code_locations = get_code_location_manager()

        # Before any Dagster subprocess exists, and in BOTH modes: code
        # locations are spawned without ``--instance-ref`` (see
        # ``CodeLocationManager._spawn_grpc``) and inherit DAGSTER_HOME from
        # ``build_dagster_env``, so each one resolves ``DagsterInstance.get()``
        # against this home — remote mode included, where no daemon or
        # webserver runs locally but code locations still do.
        self._prepare_dagster_home()
        await self._migrate_instance()

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

        # Local mode: an initial (possibly empty) workspace.yaml, then daemon +
        # webserver pointing at it. The home itself is already prepared above.
        workspace_yaml = code_locations.ensure_workspace_yaml()

        # Both processes load the SAME workspace.yaml the CodeLocationManager
        # keeps in sync, which is how a location registered later becomes
        # visible to the instance without restarting either.
        daemon = (
            self._spawn_guarded(
                DAEMON_PROCESS, ["dagster-daemon", "run", "-w", str(workspace_yaml)]
            )
            if self._settings.dagster_daemon_enabled
            else False
        )
        webserver = (
            self._spawn_guarded(
                WEBSERVER_PROCESS,
                [
                    "dagster-webserver",
                    "-h", self._settings.dagster_webserver_host,
                    "-p", str(self._settings.dagster_webserver_port),
                    "-w", str(workspace_yaml),
                ],
            )
            if self._settings.dagster_webserver_enabled
            else False
        )
        self._start_supervisor()
        logger.info(
            "dagster_process_manager_local_mode_started",
            daemon=daemon,
            webserver=webserver,
        )

    def _spawn_guarded(self, name: str, command: list[str]) -> bool:
        """Spawn one instance-level process, reporting failure instead of raising.

        These processes are a data plane this service SUPERVISES, not a
        precondition for serving it: SQLMesh, Git, files and leases all work
        without them, so a missing or broken Dagster binary must degrade the
        asset DAG, never take the runtime down with it.

        Guarded PER PROCESS for the same reason: one unavailable binary would
        otherwise abort the whole startup sequence and keep the other one from
        ever being started.
        """
        try:
            self.spawn(name, command)
            return True
        except ProcessStartError as exc:
            logger.error(
                "dagster_process_start_failed",
                name=name,
                error=str(exc),
                # str(ServiceError) is the message only; without this the cause
                # (a missing executable, a bad PATH) never reaches the log and
                # the operator sees "failed to start" with nothing to act on.
                detail=exc.detail,
                effect="Dagster schedules and the asset DAG are unavailable; "
                "the rest of this service is unaffected",
            )
            return False

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
                logger.error(
                    "dagster_process_respawn_failed",
                    name=proc.name,
                    error=str(e),
                    detail=e.detail,
                )
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

    async def _migrate_instance(self) -> bool:
        """Provision/upgrade the local instance storage, serially, up front.

        Returns whether the storage is known-good afterwards.

        Why this exists
        ---------------
        Dagster's SQLite storages stamp their alembic revision on FIRST USE, and
        that path is not concurrency-safe. Each storage reads ``alembic_version``
        and, finding it empty, picks a revision by checking whether its data
        tables already exist:

        - tables absent  -> ``stamp head``, the correct revision for a new
          database;
        - tables present -> ``stamp LAST_KNOWN_STAMPED_SQLITE_ALEMBIC_REVISION``,
          a compatibility branch for databases created before Dagster PR #25740.

        ``create_all()`` runs in between, and it is DDL — which pysqlite commits
        immediately, while the stamp commits later. A second process therefore
        observes "tables present, version table empty", takes the compatibility
        branch, and inserts a SECOND, unrelated revision. ``alembic_version``
        ends up holding two rows, and from that moment every Dagster process —
        the daemon, the webserver, and every code location gRPC server — dies
        during startup with "Version table 'alembic_version' has more than one
        head present". The user-visible symptom is an asset DAG that is
        permanently empty while this service reports itself healthy, because a
        code location that cannot start is a soft failure by design.

        None of that is hypothetical: ``start()`` spawns the daemon and the
        webserver back to back, sub-millisecond apart, which lands both inside
        the window on every fresh DAGSTER_HOME.

        Running Dagster's own ``dagster instance migrate`` first serialises the
        provisioning, so every subprocess spawned afterwards opens an already
        initialised home and never reaches the racy branch. Deliberately the
        official command rather than a hand-rolled stamp: the latter would fork
        the truth about which revision is head into this codebase.
        """
        home = self._dagster_home()
        external = self._external_storage_key(home)
        if external is not None:
            logger.info(
                "dagster_instance_migrate_skipped",
                home=home,
                storage_key=external,
                reason="dagster.yaml points instance storage at a database this "
                "replica does not own",
            )
            return False

        command = ["dagster", "instance", "migrate"]
        try:
            # to_thread, not create_subprocess_exec: this runs once during
            # startup and subprocess.run's timeout also kills the child, which
            # the asyncio equivalent would leave to the caller.
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                env={**os.environ, **self.build_dagster_env()},
                capture_output=True,
                text=True,
                timeout=INSTANCE_MIGRATE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._log_migrate_failure(home, command, error=str(exc))
            return False

        if completed.returncode != 0:
            self._log_migrate_failure(
                home,
                command,
                returncode=completed.returncode,
                # stdout as fallback: the Dagster CLI reports some failures
                # there, and an error log with no cause is not actionable.
                error=(completed.stderr or completed.stdout or "").strip()[-2000:],
            )
            return False

        logger.info("dagster_instance_migrated", home=home)
        return True

    def _log_migrate_failure(self, home: str, command: list[str], **fields) -> None:
        """Report a failed pre-flight migration. Never fatal.

        Same reasoning as :meth:`_spawn_guarded`: Dagster is a data plane this
        service supervises, not a precondition for serving it — SQLMesh, Git,
        files and leases all work without it. Logged at error level with a
        remediation because the consequence is DELAYED: the subprocesses spawned
        next can still corrupt the storage, and that failure surfaces far from
        here as an empty asset DAG.
        """
        logger.error(
            "dagster_instance_migrate_failed",
            home=home,
            command=" ".join(command),
            effect="Dagster subprocesses may race-provision the instance storage "
            "and leave it permanently unopenable",
            remediation="If Dagster later reports \"alembic_version has more than "
            "one head present\", reset the instance storage: stop this service, "
            "delete the history/ and schedules/ directories under DAGSTER_HOME, "
            "then restart",
            **fields,
        )

    def _external_storage_key(self, home: str) -> Optional[str]:
        """The ``dagster.yaml`` key pointing storage outside this replica, if any.

        Guard for :meth:`_migrate_instance`. With no ``dagster.yaml`` Dagster
        provisions SQLite under DAGSTER_HOME, and migrating that is a local,
        idempotent act on a database this replica alone opens. A ``dagster.yaml``
        that configures storage names something else — in remote mode, a database
        owned by the control plane and shared by every replica — and running
        alembic against it from N data-plane replicas concurrently is a strictly
        worse failure than the race being prevented. Skip, and say why.
        """
        path = Path(home) / "dagster.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, yaml.YAMLError) as exc:
            # Unreadable: Dagster itself will fail on this file loudly enough.
            # Since we cannot tell which storage it names, not migrating is the
            # conservative choice.
            logger.warning("dagster_yaml_unreadable", path=str(path), error=str(exc))
            return "unreadable"
        if not isinstance(raw, dict):
            return None
        return next((key for key in EXTERNAL_STORAGE_KEYS if raw.get(key)), None)


_manager: Optional[DagsterProcessManager] = None


def get_process_manager() -> DagsterProcessManager:
    """Return the process-wide manager singleton."""
    global _manager
    if _manager is None:
        _manager = DagsterProcessManager()
    return _manager
