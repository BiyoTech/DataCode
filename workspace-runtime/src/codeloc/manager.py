"""Code Location gRPC servers: the mechanism behind a Dagster deployment.

A Code Location is a gRPC server process that loads one working copy's user
Python code from ``{workspace_path}/asset-dag/definitions.py``. It is ALWAYS
local, because the code lives on the local filesystem.

This module owns the MECHANISM only:

- start / reload / stop a gRPC server for a given location name,
- keep ``DAGSTER_HOME/workspace.yaml`` in sync so the local daemon and webserver
  discover the running servers,
- allocate and PERSIST the gRPC port of each location,
- track, in remote mode, whether the CONTROL PLANE has confirmed a location so
  the registration channel (see :mod:`src.api.rest.internal`) can report what is
  still waiting to be picked up.

It deliberately no longer decides *what a location is called*. The name (and the
deployment identity injected into the process) is passed in by
:mod:`src.core.deployment_manager`, which derives it from the platform's naming
algebra. Deriving a name from the working-copy path — as this module used to —
produced ``ws_envs_{env}`` under the environment layout: a name carrying no
workspace and colliding across workspaces.

Port allocation is SEGMENTED per replica (see :mod:`src.core.port_allocation`):
each replica claims an exclusive slice of the port space via a Redis lease
(stable-hash fallback when Redis is down) and assigns ports only inside it.
Assignments are persisted to a PER-REPLICA shard file
(``DAGSTER_HOME/code_location_ports.{replica_id}.json``) so replicas sharing a
filesystem each write their own file — no write races, no leader election. A
restart reclaims its previous segment, so every location keeps its port.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from datacode_common.python_deps import site_packages_path

from src.codeloc.errors import CodeLocationLoadError, WorkspacePathInvalidError
from src.codeloc.pod import WORKSPACE_MOUNT_PATH as POD_WORKSPACE_MOUNT_PATH
from src.codeloc.ports import PortSegmentAllocator
from src.codeloc.scaffold import ASSET_DAG_SUBDIR, ensure_asset_dag_scaffold
from src.config import Settings, settings
from src.deps.manifest import overlay_for_injection
from src.utils.logging import get_logger

logger = get_logger(__name__)

DEFINITIONS_FILE = "definitions.py"

# Legacy single-replica port state (pre-segmentation). Still read once so an
# upgraded replica keeps the assignments that fall inside its segment.
_PORT_STATE_FILE = "code_location_ports.json"


@dataclass(frozen=True)
class CodeLocationSpec:
    """Everything the manager needs to run one code location.

    ``env`` is the deployment identity the loaded user code reads (see
    ``src/templates/defs/deployment.py.tpl``); the manager passes it through
    without interpreting it.
    """

    location_name: str
    workspace_path: str
    workspace_name: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CodeLocationAdvertisement:
    """One code location as offered to the control plane for registration.

    ``host``/``port`` are what the control plane dials, so ``host`` is the
    ADVERTISED address (see ``Settings.advertised_code_location_host``), never the
    bind address.
    """

    location_name: str
    host: str
    port: int
    revision: int
    acknowledged: bool

    def as_dict(self) -> dict:
        return {
            "location_name": self.location_name,
            "host": self.host,
            "port": self.port,
            "revision": self.revision,
            "acknowledged": self.acknowledged,
        }


@dataclass
class CodeLocationRuntime:
    """Where a running code location can be reached."""

    location_name: str
    workspace_path: str
    host: str
    port: int
    definitions: str
    alive: bool = True

    def as_dict(self) -> dict:
        return {
            "location_name": self.location_name,
            "workspace_path": self.workspace_path,
            "host": self.host,
            "port": self.port,
            "definitions": self.definitions,
            "alive": self.alive,
        }


class _LocationEntry:
    """Bookkeeping for a single running code location.

    ``revision`` increments on every (re)spawn and is what the control plane
    acknowledges. Comparing the acknowledged revision with the current one is how
    a reload that the control plane has not yet picked up is distinguished from
    one it already serves — a boolean "registered" flag cannot express that, and
    would leave a reloaded location reported as up-to-date while the control
    plane still holds the previous process's endpoint.
    """

    def __init__(self, spec: CodeLocationSpec, port: int):
        self.spec = spec
        self.port = port
        self.revision = 0
        # Highest revision the control plane confirmed, and when. 0 == never.
        self.acknowledged_revision = 0
        self.acknowledged_at: Optional[float] = None
        # Pod vehicle only. ``host`` is the Service FQDN, which survives Pod
        # recreation; ``pod_ready`` is the last observed Endpoints state (the
        # registry), refreshed by :meth:`CodeLocationManager.refresh_state`.
        self.pod_name = ""
        self.host = ""
        self.pod_ready = False

    @property
    def location_name(self) -> str:
        return self.spec.location_name

    @property
    def workspace_path(self) -> str:
        return self.spec.workspace_path

    @property
    def is_acknowledged(self) -> bool:
        """Whether the control plane confirmed THIS revision of the location."""
        return self.revision > 0 and self.acknowledged_revision >= self.revision


class CodeLocationManager:
    """Runs per-deployment gRPC Code Location servers + workspace.yaml."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings
        # Keyed by location name: one deployment, one location, one vehicle.
        self._locations: dict[str, _LocationEntry] = {}
        # This replica's port assignments, populated lazily once the port
        # SEGMENT is claimed (see _ensure_segment): the segment decides which
        # persisted entries belong to us. Pod mode needs none of it.
        self._ports: dict[str, int] = {}
        self._allocator = PortSegmentAllocator(cfg)
        self._pods: Optional[Any] = None

    @property
    def vehicle(self) -> str:
        """Which vehicle runs code locations: ``process`` or ``pod``."""
        return (self._settings.code_location_mode or "process").strip().lower()

    def _pod_backend(self) -> Any:
        """The Pod vehicle, constructed on first use.

        Lazy because it needs the Kubernetes client, an optional dependency a
        dev install omits — importing it at module scope would break process
        mode on every machine without a cluster.
        """
        if self._pods is None:
            from src.codeloc.pod import CodeLocationPodBackend

            self._pods = CodeLocationPodBackend()
        return self._pods

    def _environment_of(self, spec: CodeLocationSpec) -> str:
        """The Dagster environment a location serves.

        Read from the deployment identity the caller injected rather than
        parsed out of the location name: the name is the platform's naming
        algebra, and re-deriving from it here would couple this module to it.
        """
        return str(spec.env.get("DATACODE_ENVIRONMENT") or "")

    # --- Public API ----------------------------------------------------------

    async def ensure(self, spec: CodeLocationSpec) -> CodeLocationRuntime:
        """Ensure a running gRPC Code Location for ``spec``.

        Idempotent: an already-alive server is left untouched (respawning a
        healthy location would kill in-flight GraphQL calls and briefly make the
        webserver answer 5xx for it), but its spec is refreshed so a later
        reload uses the caller's current deployment identity. Use :meth:`reload`
        to force a re-import after code changes.
        """
        await self._ensure_segment()
        definitions = self._require_definitions(spec)

        entry = self._locations.get(spec.location_name)
        if entry is None:
            # Pod mode has no port to allocate: each Pod listens on one fixed
            # port inside its own network namespace.
            if self.vehicle == "pod":
                from src.codeloc.pod import CODELOC_PORT

                port = CODELOC_PORT
            else:
                port = self._allocate_port(spec.location_name)
            entry = _LocationEntry(spec, port=port)
            self._locations[spec.location_name] = entry
        else:
            entry.spec = spec

        if self.vehicle == "pod":
            # Aliveness of a Pod is the Endpoints registry, which is only as
            # fresh as the last poll; refresh before deciding.
            await self.refresh_state()
            if self.is_alive(spec.location_name):
                logger.info(
                    "code_location_already_running",
                    location=entry.location_name,
                    host=entry.host,
                )
                return self._runtime(entry, definitions, alive=True)
            await self._start_pod(entry)
            self._write_workspace_yaml()
            await self._propagate(entry)
            return self._runtime(entry, definitions, alive=entry.pod_ready)

        if self.is_alive(spec.location_name):
            logger.info(
                "code_location_already_running",
                location=entry.location_name,
                port=entry.port,
            )
            return self._runtime(entry, definitions, alive=True)

        self._spawn_grpc(entry, definitions)
        # Block until the freshly spawned server accepts connections so the
        # subsequent webserver reload (and any immediate asset query) does not
        # race an unbound port and get RepositoryNotFoundError.
        alive = await self._wait_grpc_ready(entry)
        self._write_workspace_yaml()
        await self._propagate(entry)
        return self._runtime(entry, definitions, alive=alive)

    async def reload(self, spec: CodeLocationSpec) -> CodeLocationRuntime:
        """Re-import a code location's user code."""
        await self._ensure_segment()
        entry = self._locations.get(spec.location_name)
        if entry is None:
            # Never started (or started before a restart): starting it IS the
            # reload, and it picks up the current code by definition.
            return await self.ensure(spec)

        entry.spec = spec
        definitions = self._require_definitions(spec)
        if self.vehicle == "pod":
            # A Pod's user code is baked into what it loaded at start-up, so the
            # only way to re-import is to replace it. The Service name is
            # deterministic, so the control plane's endpoint stays valid — but
            # the revision still moves, because what it now dials is a different
            # process.
            await self._start_pod(entry)
            self._write_workspace_yaml()
            await self._propagate(entry)
            return self._runtime(entry, definitions, alive=entry.pod_ready)
        # Wait for the new process to listen BEFORE asking the webserver to
        # reload — otherwise the reload hits the still-terminating old port and
        # fails with UNAVAILABLE, leaving the location unloaded while the
        # endpoint reports success.
        self._spawn_grpc(entry, definitions)
        alive = await self._wait_grpc_ready(entry)
        self._write_workspace_yaml()
        await self._propagate(entry)
        return self._runtime(entry, definitions, alive=alive)

    async def remove(self, location_name: str) -> bool:
        """Stop a code location and drop it from workspace.yaml.

        The port assignment is kept: the same deployment recreated later should
        come back on the same port. Returns whether anything was running.

        Async because stopping a Pod is a cluster round trip; the process
        vehicle's stop is synchronous underneath and awaited here for one
        signature across both modes.
        """
        entry = self._locations.pop(location_name, None)
        if entry is None:
            return False

        if self.vehicle == "pod" and entry.pod_name:
            await self._pod_backend().stop(
                entry.pod_name,
                self._service_name_of(entry),
            )
        elif self.vehicle != "pod":
            from src.codeloc.process import get_process_manager

            get_process_manager().stop(self._process_name(location_name))

        self._write_workspace_yaml()
        logger.info(
            "code_location_removed",
            location=location_name,
            port=entry.port,
            vehicle=self.vehicle,
        )
        return True

    async def refresh_state(self) -> None:
        """Re-read every Pod-mode location's aliveness from the cluster.

        Pod aliveness is the Endpoints object behind the location's Service, and
        reading it is a network call — so it cannot happen inside
        :meth:`is_alive`, which is synchronous and called from request paths.
        This is the refresh point: the registration endpoints await it before
        building an advertisement, so what the control plane is told has just
        been checked rather than remembered.
        """
        if self.vehicle != "pod":
            return
        backend = self._pod_backend()
        for entry in self._locations.values():
            service = self._service_name_of(entry)
            if not service:
                continue
            entry.pod_ready = await backend.is_ready(service)

    def _service_name_of(self, entry: _LocationEntry) -> str:
        """Headless Service name of a Pod-mode location, empty if unknown."""
        environment = self._environment_of(entry.spec)
        if not environment:
            return ""
        from src.codeloc.pod import service_name

        return service_name(environment)

    async def _start_pod(self, entry: _LocationEntry) -> None:
        """Create (or replace) the location's Pod. Pod mode only."""
        environment = self._environment_of(entry.spec)
        if not environment:
            raise CodeLocationLoadError(
                entry.workspace_path,
                detail="pod-mode code location needs DATACODE_ENVIRONMENT in its "
                       "deployment identity (the Service name is derived from it)",
            )
        pod, host, port = await self._pod_backend().start(
            entry.location_name,
            entry.workspace_path,
            environment,
            env=self._code_location_env(entry),
        )
        # Delete-then-create inside the backend means a reload leaves a new Pod
        # under the same deterministic name; the Service (and therefore the
        # advertised address) is unchanged, but the revision must move because
        # what the control plane will dial is a different process.
        entry.pod_name = pod
        entry.host = host
        entry.port = port
        entry.pod_ready = True
        entry.revision += 1
        logger.info(
            "code_location_pod_replaced",
            location=entry.location_name,
            environment=environment,
            pod=pod,
            host=host,
            revision=entry.revision,
        )

    def ensure_workspace_yaml(self) -> Path:
        """Write the current workspace.yaml (possibly empty) and return its path.

        Called at startup so the daemon and webserver have a file to load even
        before any code location exists.
        """
        return self._write_workspace_yaml()

    def list(self) -> list[CodeLocationRuntime]:
        """Every code location this service currently manages."""
        return [
            self._runtime(
                entry,
                self._definitions_path(entry.workspace_path),
                alive=self.is_alive(entry.location_name),
            )
            for entry in self._locations.values()
        ]

    def get(self, location_name: str) -> Optional[CodeLocationRuntime]:
        entry = self._locations.get(location_name)
        if entry is None:
            return None
        return self._runtime(
            entry,
            self._definitions_path(entry.workspace_path),
            alive=self.is_alive(location_name),
        )

    def is_alive(self, location_name: str) -> bool:
        """Whether this location's vehicle is currently serving.

        Process mode asks the supervisor whether the pid lives. Pod mode reads
        the last observed Endpoints state — refreshed by :meth:`refresh_state`,
        not here, because a cluster read cannot happen on a synchronous request
        path without blocking the event loop.
        """
        entry = self._locations.get(location_name)
        return entry is not None and self._is_serving(entry)

    def _is_serving(self, entry: _LocationEntry) -> bool:
        """Whether a location answers gRPC right now, from its entry.

        :meth:`is_alive` resolved by name; this is the same answer for callers
        that already hold the entry, which is every serialisation path.
        """
        if self.vehicle == "pod":
            return bool(entry.pod_ready)
        proc = self._process_of(entry)
        return proc is not None and proc.is_alive()

    def _process_of(self, entry: _LocationEntry) -> Any:
        """The supervised OS process of a location, or None in Pod mode.

        Typed ``Any`` rather than ``ManagedProcess``: importing that type here
        would make this module depend on ``process`` at import time, and
        ``process`` already imports this module lazily inside ``start()``.
        """
        if self.vehicle == "pod":
            return None
        from src.codeloc.process import get_process_manager

        return get_process_manager().get(self._process_name(entry.location_name))

    def _exit_detail(self, entry: _LocationEntry) -> dict:
        """How a location's process died, as log fields (empty while it runs).

        A negative status is a signal, and naming it is the difference between
        "it exited" and "something killed it": -9 is an OOM kill or a container
        memory limit, which reads nothing like a Python traceback, and -1 with
        no output at all is usually a crash during interpreter start-up.
        """
        # Diagnostic only, and reached on the failure path: a vehicle that
        # cannot report a status costs the log a field, never the caller its
        # answer. Enriching a warning must not be able to break the code path
        # that emits it.
        reader = getattr(self._process_of(entry), "exit_code", None)
        status = reader() if callable(reader) else None
        if status is None:
            return {}
        if status >= 0:
            return {"exit_code": status}
        number = -status
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = f"signal {number}"
        return {"exit_code": status, "terminated_by": name}

    @staticmethod
    def _location_log_path(entry: _LocationEntry) -> str:
        """Where this location's stdout/stderr is written.

        Inside the working copy it serves, under the platform's ``logs/`` root:
        the transcript belongs to that copy (it is the user's own code talking),
        travels with it when the lease moves, and is readable by the agent with
        the file tools it already has.
        """
        from src.files.layout import codeloc_logs_dir

        return str(Path(codeloc_logs_dir(entry.spec.workspace_path)) / "server.log")

    def load_failure(self, location_name: str) -> dict:
        """Why a location is not serving, as structured facts. Empty when it is.

        Used by the reload job to explain a failure instead of reporting
        ``alive=false`` with no reason — which is what this endpoint did before,
        leaving the UI with nothing to show.

        Returns:
            ``{}`` while the location serves, otherwise ``exit_code`` /
            ``terminated_by`` when the process died, plus ``output_tail`` with the
            end of its transcript (the import traceback, when there is one).
        """
        entry = self._locations.get(location_name)
        if entry is None or self._is_serving(entry):
            return {}

        detail: dict = dict(self._exit_detail(entry))
        proc = self._process_of(entry)
        tail = proc.output_tail() if proc is not None else ""
        if not tail:
            # Pod mode, or a process object we no longer hold: read the file
            # directly, since the transcript outlives whatever wrote it.
            tail = self._read_log_tail(entry)
        if tail:
            detail["output_tail"] = tail
        return detail

    @staticmethod
    def _read_log_tail(entry: _LocationEntry, max_bytes: int = 8192) -> str:
        """Tail of a location's transcript file, independent of its process."""
        path = Path(CodeLocationManager._location_log_path(entry))
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def port_of(self, location_name: str) -> Optional[int]:
        """Port assigned to a location, whether or not it is running."""
        return self._ports.get(location_name)

    # --- Registration channel (remote mode) ----------------------------------

    def replica_id(self) -> str:
        """Stable identifier of this service replica.

        The control plane aggregates advertisements from every data plane replica
        and must be able to tell whose entries it is replacing on each poll.
        Falls back to the hostname, which is the container name under compose and
        the Pod name under K8s — both unique within a deployment.
        """
        return os.getenv("DATACODE_REPLICA_ID") or socket.gethostname()

    def advertisements(self) -> list[CodeLocationAdvertisement]:
        """Reachable code locations this replica offers to the control plane.

        Only ALIVE locations with a routable advertised host are listed: an entry
        the control plane cannot dial makes its whole workspace load fail, which
        would take down the code locations that ARE healthy alongside it. A
        location that is down is therefore omitted rather than advertised as
        broken — omission is a state the control plane can act on, a dead
        endpoint is not.
        """
        if self.vehicle == "pod":
            # No configured advertise host is needed or wanted: a Pod-mode
            # location is reached through its Service FQDN, which is computed
            # per entry below. Requiring a host here would silently advertise
            # nothing in the very mode where the address is already known.
            host = ""
        else:
            host = self._settings.advertised_code_location_host
            if not host:
                # Logged once per poll on purpose: a replica that silently
                # advertises nothing is indistinguishable from one with no
                # deployments.
                logger.warning(
                    "code_location_advertise_host_unset",
                    bind_host=self._settings.code_location_host,
                    locations=len(self._locations),
                )
                return []

        out: list[CodeLocationAdvertisement] = []
        for entry in self._locations.values():
            if not self.is_alive(entry.location_name):
                continue
            out.append(
                CodeLocationAdvertisement(
                    location_name=entry.location_name,
                    # Pod mode ignores the configured advertise host: the
                    # Service FQDN is by definition how the cluster reaches the
                    # location, and a configured host would be a second answer
                    # to the same question.
                    host=entry.host if self.vehicle == "pod" else host,
                    port=entry.port,
                    revision=entry.revision,
                    acknowledged=entry.is_acknowledged,
                )
            )
        return out

    def acknowledge(self, acknowledged: dict[str, int]) -> list[str]:
        """Record which location revisions the control plane now serves.

        Args:
            acknowledged: ``{location_name: revision}`` as reported back by the
                control plane after it loaded them.

        Returns:
            The names it acknowledged that this replica does not know, so a stale
            control-plane entry (a destroyed deployment it still lists) surfaces
            instead of being silently dropped.
        """
        unknown: list[str] = []
        now = time.time()
        for name, revision in acknowledged.items():
            entry = self._locations.get(name)
            if entry is None:
                unknown.append(name)
                continue
            # Never move the watermark backwards: acknowledgements may arrive out
            # of order when several control-plane pollers overlap.
            if revision > entry.acknowledged_revision:
                entry.acknowledged_revision = revision
            entry.acknowledged_at = now
        if unknown:
            logger.warning("code_location_acknowledged_unknown", locations=unknown)
        return unknown

    def pending_registrations(self) -> list[str]:
        """Alive locations the control plane has not confirmed at their current
        revision. Surfaced through ``/health`` so a broken registration channel
        is visible before a user reports missing assets."""
        return [
            entry.location_name
            for entry in self._locations.values()
            if self.is_alive(entry.location_name) and not entry.is_acknowledged
        ]

    # --- Internals -----------------------------------------------------------

    def _runtime(
        self, entry: _LocationEntry, definitions: Path, *, alive: bool
    ) -> CodeLocationRuntime:
        # Pod mode advertises the Service FQDN: the Pod's own IP is worthless
        # to a caller outside this namespace, and it changes on every respawn.
        host = entry.host if self.vehicle == "pod" else self._settings.code_location_host
        return CodeLocationRuntime(
            location_name=entry.location_name,
            workspace_path=entry.workspace_path,
            host=host,
            port=entry.port,
            definitions=str(definitions),
            alive=alive,
        )

    def _require_definitions(self, spec: CodeLocationSpec) -> Path:
        """Scaffold the asset-dag project if needed and return definitions.py."""
        asset_dag = self._asset_dag_path(spec.workspace_path)
        # Fill in any missing scaffold file (self-heals broken working copies)
        # and refresh the managed bridge/deployment files; never overwrites
        # user-authored code.
        ensure_asset_dag_scaffold(asset_dag, workspace_name=spec.workspace_name)
        definitions = asset_dag / DEFINITIONS_FILE
        if not definitions.exists():
            raise CodeLocationLoadError(
                spec.workspace_path,
                detail=f"Missing {ASSET_DAG_SUBDIR}/{DEFINITIONS_FILE} in workspace",
            )
        return definitions

    async def _wait_grpc_ready(self, entry: _LocationEntry) -> bool:
        """Poll the location's gRPC port until it accepts connections.

        ``dagster api grpc`` needs ~1-2s to import user code and bind its port.
        A liveness check on the OS process is not enough — the pid exists long
        before the port is listening. Callers that spawn or reload must wait here
        so the downstream webserver reload and asset queries see a bound port
        instead of racing into ``Connection refused`` / ``RepositoryNotFoundError``.

        Connects to ``localhost`` (the reachable loopback) rather than the bind
        host, which may be ``0.0.0.0``. Returns True once ready; on timeout logs
        a warning and returns False so the caller can proceed with the existing
        soft-fail fallbacks rather than hard-erroring during a slow load.
        """
        deadline = asyncio.get_event_loop().time() + self._settings.process_start_timeout
        attempt = 0
        while asyncio.get_event_loop().time() < deadline:
            # Bail out early if the subprocess died on startup (bad user code,
            # port clash) — no point polling a port that will never open.
            if not self.is_alive(entry.location_name):
                logger.warning(
                    "code_location_grpc_process_exited",
                    location=entry.location_name,
                    port=entry.port,
                    # exit_code is what distinguishes the two families of cause:
                    # a Python-level failure (1, with a traceback on stderr) from
                    # the process being killed (-9, with nothing at all).
                    **self._exit_detail(entry),
                )
                return False
            attempt += 1
            try:
                _, writer = await asyncio.open_connection("localhost", entry.port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 - close errors are irrelevant
                    pass
                logger.info(
                    "code_location_grpc_ready",
                    location=entry.location_name,
                    port=entry.port,
                    attempts=attempt,
                )
                return True
            except (ConnectionRefusedError, OSError):
                # Port not listening yet; back off briefly and retry.
                await asyncio.sleep(0.2)
        logger.warning(
            "code_location_grpc_ready_timeout",
            location=entry.location_name,
            port=entry.port,
            timeout=self._settings.process_start_timeout,
            # The process is still alive but never bound its port — almost always
            # user code that hangs at import. Its own output is the only clue, so
            # it goes in the log line rather than only in the file.
            output_tail=self._read_log_tail(entry, max_bytes=2048),
        )
        return False

    def _spawn_grpc(self, entry: _LocationEntry, definitions: Path) -> None:
        """(Re)start the gRPC Code Location server of one deployment.

        The ``dagster`` executable is resolved from THIS service's interpreter,
        not from a per-workspace venv: one well-known interpreter serves every
        code location. The process manager puts that interpreter's ``bin/`` at
        the front of the child's PATH (see ``process.dagster_search_path``) —
        relying on the ambient PATH would break the dev flow, which runs this
        service from a virtualenv it never activates.
        ``cwd`` is the asset-dag directory, which Dagster puts on sys.path so
        the ``defs`` package (auto-discovery root) imports resolve.

        The process is registered with an ``on_restart`` hook so the supervisor
        can respawn a crashed location and re-attach it to the webserver without
        anyone re-issuing an ensure.
        """
        from src.codeloc.process import get_process_manager

        command = [
            "dagster", "api", "grpc",
            "-h", self._settings.code_location_host,
            "-p", str(entry.port),
            "-f", str(definitions),
            "--location-name", entry.location_name,
        ]
        get_process_manager().spawn(
            self._process_name(entry.location_name),
            command,
            cwd=str(definitions.parent),
            env=self._code_location_env(entry),
            on_restart=lambda: self._after_restart(entry),
            # The transcript of the USER's code. Its last lines are the import
            # traceback when definitions fail to load, which is the only
            # actionable content a failed reload has.
            log_path=self._location_log_path(entry),
        )
        # A new process means the control plane's acknowledgement (which refers to
        # the previous one) no longer describes what is running here.
        entry.revision += 1
        logger.info(
            "code_location_grpc_started",
            location=entry.location_name,
            port=entry.port,
            revision=entry.revision,
            definitions=str(definitions),
        )

    async def _after_restart(self, entry: _LocationEntry) -> None:
        """Re-attach a supervisor-respawned location to the Dagster instance."""
        alive = await self._wait_grpc_ready(entry)
        if not alive:
            # Left out of workspace.yaml on purpose: asking the webserver to
            # reload an endpoint that never came up just moves the failure to a
            # place where it looks like a network problem.
            logger.warning(
                "code_location_restart_not_serving",
                location=entry.location_name,
                port=entry.port,
                effect="omitted from workspace.yaml; the asset DAG ignores this "
                "location until it is ensured again",
            )
            return
        # The crash is what took it out of workspace.yaml, and nothing else
        # writes it back: a location the supervisor revived on its own would
        # otherwise stay invisible to the webserver forever.
        self._write_workspace_yaml()
        await self._propagate(entry)
        logger.info(
            "code_location_reattached_after_restart",
            location=entry.location_name,
            port=entry.port,
        )

    async def _propagate(self, entry: _LocationEntry) -> None:
        """Make the (local or remote) DagsterInstance aware of the location.

        The two modes reach the instance by opposite mechanisms:

        * **local** — this service owns the webserver's ``workspace.yaml``, so it
          PUSHES: write the file, then ask the webserver to reload.
        * **remote** — the control plane PULLS from ``/internal/code-locations``
          and reloads itself once it has rendered its own workspace file. Pushing
          from here would be actively harmful: the location is absent from the
          control plane's file, so the reload would fall back to
          ``reloadWorkspace`` and re-load EVERY other replica's locations — a
          full control-plane reload per deployment — while still not registering
          this one.
        """
        from src.codeloc.client import get_dagster_client

        if self._settings.is_remote:
            logger.info(
                "code_location_awaiting_control_plane_registration",
                location=entry.location_name,
                revision=entry.revision,
                grpc_endpoint=self._grpc_endpoint(entry),
                remote=self._settings.active_graphql_url,
            )
            return

        # Best-effort reload; failures are non-fatal (webserver may still be
        # booting). The webserver picks up new locations from workspace.yaml.
        try:
            await get_dagster_client().reload_code_location(entry.location_name)
        except Exception as e:  # noqa: BLE001 - reload is best-effort
            logger.warning(
                "code_location_reload_skipped", location=entry.location_name, error=str(e)
            )

    def _grpc_endpoint(self, entry: _LocationEntry) -> str:
        """Dialable endpoint of a location, for logs and operator notes."""
        host = self._settings.advertised_code_location_host or self._settings.code_location_host
        return f"grpc://{host}:{entry.port}"

    def _write_workspace_yaml(self) -> Path:
        """Serialize the SERVING locations into DAGSTER_HOME/workspace.yaml.

        Only locations that are actually up are listed. A dead one in this file
        is worse than an absent one: the webserver loads it on every request,
        cannot reach the port, and answers ``Could not reach user code server``
        — a network-shaped error for what is almost always a crashed import in
        user code. The operator then debugs connectivity while the real cause
        sits in a subprocess stderr nobody is reading.

        Dropping one is not a loss of state: the supervisor's restart hook adds
        it back once it serves again (see :meth:`_after_restart`), so the file
        converges on what is reachable rather than on what was once started.

        Written in remote mode too, although no local process reads it: it is the
        operator's view of what this replica runs, and it makes a mode switch
        (remote -> local) work without a reload dance.
        """
        home = Path(self._settings.dagster_home).expanduser()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "workspace.yaml"

        serving: list[_LocationEntry] = []
        omitted: list[str] = []
        for entry in self._locations.values():
            # Partitioned once: polling liveness twice could disagree if the
            # process dies in between, and a location must never land in both.
            if self._is_serving(entry):
                serving.append(entry)
            else:
                omitted.append(entry.location_name)

        load_from = [
            {
                "grpc_server": {
                    # Pod mode: the Service FQDN, which is reachable from the
                    # webserver; the bind host would point at this replica.
                    "host": (entry.host if self.vehicle == "pod"
                             else self._settings.code_location_host),
                    "port": entry.port,
                    "location_name": entry.location_name,
                }
            }
            for entry in serving
        ]
        with open(path, "w") as f:
            yaml.safe_dump({"load_from": load_from}, f, default_flow_style=False)
        logger.info(
            "workspace_yaml_written",
            path=str(path),
            locations=len(load_from),
            # Named in the log rather than silently dropped: a location missing
            # from this file is a decision, and the operator has to be able to
            # see which one and why.
            omitted_not_serving=omitted,
        )
        return path

    def _code_location_env(self, entry: _LocationEntry) -> dict[str, str]:
        """Environment of the code location process.

        The deployment identity comes from the caller (see
        :class:`CodeLocationSpec`); only the service-wide bridge settings are
        added here. ``DAGSTER_WORKSPACE_PATH`` identifies both the workspace and
        the environment, since a working copy is per-environment.

        ``PYTHONPATH`` carries the working copy's installed third-party packages
        (injection point 1 of 3). PREPENDED, which is the only position that
        works: ``sys.path`` is ordered ``'' -> PYTHONPATH -> stdlib ->
        site-packages``, so anything appended after the image's own
        site-packages would never be reached for a name the image also has. That
        same ordering is why the platform's own packages are protected at INSTALL
        time (see :mod:`datacode_common.python_deps`) rather than here.
        """
        env = {
            "WORKSPACE_RUNTIME_URL": self._settings.sqlmesh_service_url,
            "DAGSTER_WORKSPACE_PATH": entry.workspace_path,
            # Empty -> the runtime uses its default gateway.
            "SQLMESH_GATEWAY": "",
        }

        overlay = self._overlay_python_path(entry)
        if overlay:
            env["PYTHONPATH"] = overlay

        env.update(entry.spec.env)
        return env

    def _overlay_python_path(self, entry: _LocationEntry) -> str:
        """``PYTHONPATH`` value for this location's vehicle, or ``""``.

        The DECISION is always made against the runtime's own view of the copy —
        that is where the manifest is readable. The VALUE depends on the vehicle:

        * ``process`` — a child of this service, same filesystem view, so the
          real path. The runtime's own ``PYTHONPATH`` is appended because the
          child is started from this environment and still needs whatever the
          deployment put there;
        * ``pod`` — the copy is mounted at ``/workspace`` inside the container, so
          the real path would name a directory that does not exist there. The
          runtime's own ``PYTHONPATH`` must NOT be carried over for the same
          reason: those directories belong to this image, not that one.
        """
        if not overlay_for_injection(entry.workspace_path, consumer="code_location"):
            return ""

        if self.vehicle == "pod":
            return site_packages_path(POD_WORKSPACE_MOUNT_PATH)

        overlay = site_packages_path(entry.workspace_path)
        inherited = os.environ.get("PYTHONPATH", "")
        return f"{overlay}{os.pathsep}{inherited}" if inherited else overlay

    def _asset_dag_path(self, workspace_path: str) -> Path:
        base = Path(self._settings.workspace_base_path).expanduser().resolve()
        target = Path(workspace_path).expanduser().resolve()
        # Path-traversal guard: workspace must live under the configured base.
        try:
            target.relative_to(base)
        except ValueError:
            raise WorkspacePathInvalidError(
                workspace_path, reason=f"Path is outside allowed base: {base}"
            ) from None
        return target / ASSET_DAG_SUBDIR

    def _definitions_path(self, workspace_path: str) -> Path:
        return self._asset_dag_path(workspace_path) / DEFINITIONS_FILE

    # --- Port assignment (segmented, per-replica shards) ----------------------

    async def _ensure_segment(self) -> None:
        """Claim this replica's port segment once, then load our shard.

        Idempotent and lazy: the first ``ensure``/``reload`` triggers it, later
        calls are a no-op. Loading the shard AFTER the segment is known is what
        keeps a location's port stable — only entries inside our segment are
        ours to keep; the rest are another replica's (or pre-upgrade residue).

        A no-op in Pod mode, where every location listens on one fixed port
        inside its own network namespace and there is nothing to allocate.
        """
        if self.vehicle == "pod" or self._allocator.lease is not None:
            return
        lease = await self._allocator.acquire()
        self._ports = self._load_ports()
        logger.info(
            "code_location_ports_loaded",
            segment=lease.segment,
            mode=lease.mode,
            assignments=len(self._ports),
        )

    async def shutdown(self) -> None:
        """Release the port segment lease and delete every location Pod.

        Pods are owned by this replica (they are pinned to its node and mount
        its working copy), so a graceful stop that leaves them behind strands
        user code holding a mount nobody manages. Deletion is best-effort: the
        lease release must still happen.
        """
        if self.vehicle == "pod":
            backend = self._pod_backend()
            for entry in list(self._locations.values()):
                service = self._service_name_of(entry)
                if entry.pod_name:
                    await backend.stop(entry.pod_name, service)
        await self._allocator.release()

    def _allocate_port(self, location_name: str) -> int:
        """Return this location's stable port, assigning one if it has none.

        Candidates are restricted to this replica's SEGMENT, so two replicas
        sharing a filesystem can never hand out the same port. A full segment
        is an operator problem (raise with guidance), not something to work
        around: falling through to another segment would silently reintroduce
        the cross-replica collision the segmentation exists to prevent.
        """
        existing = self._ports.get(location_name)
        if existing is not None:
            return existing

        lease = self._allocator.lease
        if lease is None:
            # Defensive: _ensure_segment runs before any allocation; reaching
            # here means a caller bypassed ensure/reload.
            raise CodeLocationLoadError(
                location_name,
                detail="Port segment not claimed before port allocation",
            )

        start, end = self._allocator.segment_port_range(lease.segment)
        taken = set(self._ports.values())
        for port in range(start, end + 1):
            if port not in taken:
                self._ports[location_name] = port
                self._save_ports()
                logger.info(
                    "code_location_port_assigned",
                    location=location_name,
                    port=port,
                    segment=lease.segment,
                    segment_mode=lease.mode,
                )
                return port

        raise CodeLocationLoadError(
            location_name,
            detail=(
                f"Port segment {lease.segment} is full "
                f"({start}-{end}). Increase dagster.code_location_port_segment_size "
                "or remove unused code locations."
            ),
        )

    def _dagster_home(self) -> Path:
        return Path(self._settings.dagster_home).expanduser()

    def _load_ports(self) -> dict[str, int]:
        """Read this replica's persisted port assignments.

        Sources, in order:

        1. This replica's shard file (``code_location_ports.{replica_id}.json``)
           — the normal case after the first segmented run.
        2. The legacy single-replica ``code_location_ports.json`` — read once
           on upgrade; only entries inside our segment are adopted so a
           restarted replica keeps its old ports, and entries outside the
           segment are left for whichever replica owns them.

        A broken file is not fatal: losing persistence only means ports are
        re-assigned inside the segment.
        """
        shard = self._read_port_file(self._dagster_home() / self._allocator.shard_file_name)
        if shard:
            return shard
        return self._adopt_legacy_ports_inside_segment()

    def _adopt_legacy_ports_inside_segment(self) -> dict[str, int]:
        """Migrate legacy single-replica assignments into this replica's segment."""
        legacy = self._read_port_file(self._dagster_home() / _PORT_STATE_FILE)
        if not legacy:
            return {}
        lease = self._allocator.lease
        assert lease is not None  # caller runs after acquire()
        start, end = self._allocator.segment_port_range(lease.segment)
        adopted = {name: port for name, port in legacy.items() if start <= port <= end}
        if adopted:
            logger.info(
                "code_location_ports_migrated_from_legacy",
                legacy_entries=len(legacy),
                adopted=len(adopted),
                segment=lease.segment,
            )
        return adopted

    def _read_port_file(self, path: Path) -> dict[str, int]:
        """Parse one port-assignment file; unreadable/broken yields {}."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("code_location_ports_unreadable", path=str(path), error=str(e))
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): int(port)
            for name, port in raw.items()
            if isinstance(port, int) or (isinstance(port, str) and port.isdigit())
        }

    def _save_ports(self) -> None:
        """Persist assignments to THIS replica's shard (no shared-file writes).

        One file per replica means replicas sharing a directory each write a
        different file: the read-modify-write race on the old shared
        ``code_location_ports.json`` is structurally impossible, which is why
        no distributed lock or leader election is needed here.
        """
        path = self._dagster_home() / self._allocator.shard_file_name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._ports, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as e:  # noqa: BLE001 - persistence is an optimization
            logger.warning("code_location_ports_unwritable", path=str(path), error=str(e))

    @staticmethod
    def _process_name(location_name: str) -> str:
        return f"code-location-{location_name}"


_manager: Optional[CodeLocationManager] = None


def get_code_location_manager() -> CodeLocationManager:
    """Return the process-wide code location manager singleton."""
    global _manager
    if _manager is None:
        _manager = CodeLocationManager()
    return _manager
