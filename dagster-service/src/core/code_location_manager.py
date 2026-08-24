"""Code Location gRPC servers: the mechanism behind a Dagster deployment.

A Code Location is a gRPC server process that loads one working copy's user
Python code from ``{workspace_path}/asset-dag/definitions.py``. It is ALWAYS
local, because the code lives on the local filesystem.

This module owns the MECHANISM only:

- start / reload / stop a gRPC server for a given location name,
- keep ``DAGSTER_HOME/workspace.yaml`` in sync so the local daemon and webserver
  discover the running servers,
- allocate and PERSIST the gRPC port of each location.

It deliberately no longer decides *what a location is called*. The name (and the
deployment identity injected into the process) is passed in by
:mod:`src.core.deployment_manager`, which derives it from the platform's naming
algebra. Deriving a name from the working-copy path — as this module used to —
produced ``ws_envs_{env}`` under the environment layout: a name carrying no
workspace and colliding across workspaces.

Port allocation is persisted to ``DAGSTER_HOME/code_location_ports.json`` so a
service restart reuses each location's previous port. Ports appear in
``workspace.yaml``, in remote-mode registrations and in operators' notes; a port
that silently moves on restart invalidates all three.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.config import Settings, settings
from src.core.scaffold import ASSET_DAG_SUBDIR, ensure_asset_dag_scaffold
from src.utils.errors import CodeLocationLoadError, WorkspacePathInvalidError
from src.utils.logging import get_logger

logger = get_logger(__name__)

DEFINITIONS_FILE = "definitions.py"

# Persisted location -> port assignments, relative to DAGSTER_HOME.
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
    """Bookkeeping for a single running code location."""

    def __init__(self, spec: CodeLocationSpec, port: int):
        self.spec = spec
        self.port = port

    @property
    def location_name(self) -> str:
        return self.spec.location_name

    @property
    def workspace_path(self) -> str:
        return self.spec.workspace_path


class CodeLocationManager:
    """Runs per-deployment gRPC Code Location servers + workspace.yaml."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings
        # Keyed by location name: one deployment, one location, one process.
        self._locations: dict[str, _LocationEntry] = {}
        self._ports: dict[str, int] = self._load_ports()

    # --- Public API ----------------------------------------------------------

    async def ensure(self, spec: CodeLocationSpec) -> CodeLocationRuntime:
        """Ensure a running gRPC Code Location for ``spec``.

        Idempotent: an already-alive server is left untouched (respawning a
        healthy location would kill in-flight GraphQL calls and briefly make the
        webserver answer 5xx for it), but its spec is refreshed so a later
        reload uses the caller's current deployment identity. Use :meth:`reload`
        to force a re-import after code changes.
        """
        definitions = self._require_definitions(spec)

        entry = self._locations.get(spec.location_name)
        if entry is None:
            entry = _LocationEntry(spec, port=self._allocate_port(spec.location_name))
            self._locations[spec.location_name] = entry
        else:
            entry.spec = spec

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
        entry = self._locations.get(spec.location_name)
        if entry is None:
            # Never started (or started before a restart): starting it IS the
            # reload, and it picks up the current code by definition.
            return await self.ensure(spec)

        entry.spec = spec
        definitions = self._require_definitions(spec)
        # Wait for the new process to listen BEFORE asking the webserver to
        # reload — otherwise the reload hits the still-terminating old port and
        # fails with UNAVAILABLE, leaving the location unloaded while the
        # endpoint reports success.
        self._spawn_grpc(entry, definitions)
        alive = await self._wait_grpc_ready(entry)
        self._write_workspace_yaml()
        await self._propagate(entry)
        return self._runtime(entry, definitions, alive=alive)

    def remove(self, location_name: str) -> bool:
        """Stop a code location and drop it from workspace.yaml.

        The port assignment is kept: the same deployment recreated later should
        come back on the same port. Returns whether anything was running.
        """
        entry = self._locations.pop(location_name, None)
        if entry is None:
            return False
        from src.core.process_manager import get_process_manager

        get_process_manager().stop(self._process_name(location_name))
        self._write_workspace_yaml()
        logger.info("code_location_removed", location=location_name, port=entry.port)
        return True

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
        """Whether this location's gRPC subprocess is currently running."""
        from src.core.process_manager import get_process_manager

        proc = get_process_manager().get(self._process_name(location_name))
        return proc is not None and proc.is_alive()

    def port_of(self, location_name: str) -> Optional[int]:
        """Port assigned to a location, whether or not it is running."""
        return self._ports.get(location_name)

    # --- Internals -----------------------------------------------------------

    def _runtime(
        self, entry: _LocationEntry, definitions: Path, *, alive: bool
    ) -> CodeLocationRuntime:
        return CodeLocationRuntime(
            location_name=entry.location_name,
            workspace_path=entry.workspace_path,
            host=self._settings.code_location_host,
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
        )
        return False

    def _spawn_grpc(self, entry: _LocationEntry, definitions: Path) -> None:
        """(Re)start the gRPC Code Location server of one deployment.

        The ``dagster`` executable is resolved from this service's own
        environment (service venv on PATH) — deliberately NOT a per-workspace
        venv, keeping one well-known interpreter for all code locations.
        ``cwd`` is the asset-dag directory, which Dagster puts on sys.path so
        the ``defs`` package (auto-discovery root) imports resolve.

        The process is registered with an ``on_restart`` hook so the supervisor
        can respawn a crashed location and re-attach it to the webserver without
        anyone re-issuing an ensure.
        """
        from src.core.process_manager import get_process_manager

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
        )
        logger.info(
            "code_location_grpc_started",
            location=entry.location_name,
            port=entry.port,
            definitions=str(definitions),
        )

    async def _after_restart(self, entry: _LocationEntry) -> None:
        """Re-attach a supervisor-respawned location to the Dagster instance."""
        await self._wait_grpc_ready(entry)
        await self._propagate(entry)
        logger.info(
            "code_location_reattached_after_restart",
            location=entry.location_name,
            port=entry.port,
        )

    async def _propagate(self, entry: _LocationEntry) -> None:
        """Make the (local or remote) DagsterInstance aware of the location."""
        from src.core.dagster_client import get_dagster_client

        if self._settings.is_remote:
            # We cannot edit the remote cluster's workspace.yaml from here. The
            # operator must register grpc://{host}:{port} on the remote side.
            # We still attempt a reload in case it is already registered.
            logger.info(
                "code_location_remote_registration_required",
                location=entry.location_name,
                grpc_endpoint=f"grpc://{self._settings.code_location_host}:{entry.port}",
                remote=self._settings.active_graphql_url,
            )

        # Best-effort reload; failures are non-fatal (webserver may still be
        # booting). The webserver picks up new locations from workspace.yaml.
        try:
            await get_dagster_client().reload_code_location(entry.location_name)
        except Exception as e:  # noqa: BLE001 - reload is best-effort
            logger.warning(
                "code_location_reload_skipped", location=entry.location_name, error=str(e)
            )

    def _write_workspace_yaml(self) -> Path:
        """Serialize all active locations into DAGSTER_HOME/workspace.yaml."""
        home = Path(self._settings.dagster_home).expanduser()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "workspace.yaml"

        load_from = [
            {
                "grpc_server": {
                    "host": self._settings.code_location_host,
                    "port": entry.port,
                    "location_name": entry.location_name,
                }
            }
            for entry in self._locations.values()
        ]
        with open(path, "w") as f:
            yaml.safe_dump({"load_from": load_from}, f, default_flow_style=False)
        logger.info("workspace_yaml_written", path=str(path), locations=len(load_from))
        return path

    def _code_location_env(self, entry: _LocationEntry) -> dict[str, str]:
        """Environment of the code location process.

        The deployment identity comes from the caller (see
        :class:`CodeLocationSpec`); only the service-wide bridge settings are
        added here. ``DAGSTER_WORKSPACE_PATH`` identifies both the workspace and
        the environment, since a working copy is per-environment.
        """
        return {
            "SQLMESH_SERVICE_URL": self._settings.sqlmesh_service_url,
            "DAGSTER_WORKSPACE_PATH": entry.workspace_path,
            # Empty -> sqlmesh-service uses its default gateway.
            "SQLMESH_GATEWAY": "",
            **entry.spec.env,
        }

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

    # --- Port assignment (persisted) -----------------------------------------

    def _allocate_port(self, location_name: str) -> int:
        """Return this location's stable port, assigning one if it has none."""
        existing = self._ports.get(location_name)
        if existing is not None:
            return existing

        taken = set(self._ports.values())
        port = self._settings.code_location_port
        while port in taken:
            port += 1
        self._ports[location_name] = port
        self._save_ports()
        logger.info("code_location_port_assigned", location=location_name, port=port)
        return port

    def _port_state_path(self) -> Path:
        return Path(self._settings.dagster_home).expanduser() / _PORT_STATE_FILE

    def _load_ports(self) -> dict[str, int]:
        """Read persisted port assignments; a broken file is not fatal.

        Losing the file only means ports are re-assigned from the base port,
        which is why it is rebuilt rather than treated as an error.
        """
        path = self._port_state_path()
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
        path = self._port_state_path()
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
