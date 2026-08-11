"""Workspace -> Dagster Code Location mapping.

A Code Location is a gRPC server process that loads a workspace's user Python
code from ``{workspace}/asset-dag/definitions.py``. It is ALWAYS local because
the code lives on the local filesystem.

Responsibilities:
- Start / reload / stop a gRPC Code Location server per workspace.
- Keep a ``workspace.yaml`` (under DAGSTER_HOME) in sync so the local daemon /
  webserver can discover the running gRPC servers.
- In remote mode, surface the local gRPC endpoint that must be registered with
  the remote DagsterInstance and best-effort trigger a reload.

Port allocation is deterministic-incremental starting from
``settings.code_location_port`` so restarts reuse stable ports where possible.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

import yaml

from src.config import Settings, settings
from src.core.scaffold import ASSET_DAG_SUBDIR, ensure_asset_dag_scaffold
from src.utils.errors import CodeLocationLoadError, WorkspacePathInvalidError
from src.utils.logging import get_logger

logger = get_logger(__name__)

DEFINITIONS_FILE = "definitions.py"


class _LocationEntry:
    """Bookkeeping for a single workspace's code location."""

    def __init__(self, workspace_path: str, location_name: str, port: int):
        self.workspace_path = workspace_path
        self.location_name = location_name
        self.port = port


class CodeLocationManager:
    """Manages per-workspace gRPC Code Location servers + workspace.yaml."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings
        self._locations: dict[str, _LocationEntry] = {}  # workspace_path -> entry
        self._next_port = self._settings.code_location_port

    # --- Public API ----------------------------------------------------------

    async def ensure_location(self, workspace_path: str) -> dict:
        """Ensure a running gRPC Code Location for ``workspace_path``.

        Idempotent: if the gRPC server is already alive this is a no-op (no
        respawn), so callers can hit it on every page load without tearing
        down a healthy code location. Use :meth:`reload_location` to force a
        re-import after user code changes.
        """
        asset_dag = self._asset_dag_path(workspace_path)
        # Fill in any missing scaffold file (self-heals legacy/broken
        # workspaces); never overwrites existing user code.
        ensure_asset_dag_scaffold(asset_dag, workspace_name=asset_dag.parent.name)
        definitions = asset_dag / DEFINITIONS_FILE
        if not definitions.exists():
            raise CodeLocationLoadError(
                workspace_path,
                detail=f"Missing {ASSET_DAG_SUBDIR}/{DEFINITIONS_FILE} in workspace",
            )

        entry = self._locations.get(workspace_path)
        if entry is None:
            entry = _LocationEntry(
                workspace_path=workspace_path,
                location_name=self._location_name(workspace_path),
                port=self._allocate_port(),
            )
            self._locations[workspace_path] = entry

        # Only (re)spawn when the server is not already running. Respawning a
        # healthy location would kill in-flight GraphQL calls and briefly make
        # the webserver return 5xx for this location.
        if not self._is_grpc_alive(entry):
            self._spawn_grpc(entry, definitions)
            # Block until the freshly spawned gRPC server accepts connections so
            # the subsequent webserver reload (and any immediate asset query)
            # does not race an unbound port and get RepositoryNotFoundError.
            await self._wait_grpc_ready(entry)
            self._write_workspace_yaml()
            await self._propagate(entry)
        else:
            logger.info(
                "code_location_already_running",
                location=entry.location_name,
                port=entry.port,
            )

        return {
            "location_name": entry.location_name,
            "host": self._settings.code_location_host,
            "port": entry.port,
            "definitions": str(definitions),
        }

    async def reload_location(self, workspace_path: str) -> dict:
        """Reload a workspace's code location after user code changes."""
        entry = self._locations.get(workspace_path)
        if entry is None:
            # Not started yet — start it now (also triggers a load).
            return await self.ensure_location(workspace_path)
        # Re-spawn the gRPC server so it re-imports the user module. Wait for
        # the new port to accept connections BEFORE asking the webserver to
        # reload — otherwise the reload hits the still-terminating old port /
        # not-yet-listening new port and fails with UNAVAILABLE, leaving the
        # location unloaded while the endpoint reports success.
        self._spawn_grpc(entry, self._definitions_path(workspace_path))
        await self._wait_grpc_ready(entry)
        await self._propagate(entry)
        return {"location_name": entry.location_name, "reloaded": True}

    def remove_location(self, workspace_path: str) -> None:
        """Stop a workspace's code location and drop it from workspace.yaml."""
        entry = self._locations.pop(workspace_path, None)
        if entry is None:
            return
        from src.core.process_manager import get_process_manager

        get_process_manager().stop(self._process_name(entry.location_name))
        self._write_workspace_yaml()

    def ensure_workspace_yaml(self) -> Path:
        """Write the current workspace.yaml (possibly empty) and return path.

        Called at startup so the daemon/webserver have a file to load even
        before any workspace code location exists.
        """
        return self._write_workspace_yaml()

    def list_locations(self) -> list[dict]:
        return [
            {"workspace_path": e.workspace_path, "location_name": e.location_name, "port": e.port}
            for e in self._locations.values()
        ]

    def location_name_for(self, workspace_path: str) -> str:
        """Public accessor for a workspace's stable code location name."""
        return self._location_name(workspace_path)

    # --- Internals -----------------------------------------------------------

    def _is_grpc_alive(self, entry: _LocationEntry) -> bool:
        """Whether this location's gRPC subprocess is currently running."""
        from src.core.process_manager import get_process_manager

        proc = get_process_manager().get(self._process_name(entry.location_name))
        return proc is not None and proc.is_alive()

    async def _wait_grpc_ready(self, entry: _LocationEntry) -> bool:
        """Poll the code location's gRPC port until it accepts connections.

        ``dagster api grpc`` needs ~1-2s to import user code and bind its port.
        A liveness check on the OS process is not enough — the pid exists long
        before the port is listening. Callers that reload/spawn must wait here so
        the downstream webserver reload and asset queries see a bound port
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
            if not self._is_grpc_alive(entry):
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
        """(Re)start the gRPC Code Location server for a workspace.

        The ``dagster`` executable is resolved from this service's own
        environment (service venv on PATH) — deliberately NOT a per-workspace
        venv, keeping one well-known interpreter for all code locations.
        ``cwd`` is the asset-dag directory, which Dagster puts on sys.path so
        the ``defs`` package (auto-discovery root) imports resolve.
        """
        from src.core.process_manager import get_process_manager

        manager = get_process_manager()
        command = [
            "dagster", "api", "grpc",
            "-h", self._settings.code_location_host,
            "-p", str(entry.port),
            "-f", str(definitions),
            "--location-name", entry.location_name,
        ]
        manager.spawn(
            self._process_name(entry.location_name),
            command,
            cwd=str(definitions.parent),
            env=self._code_location_env(entry),
        )
        logger.info(
            "code_location_grpc_started",
            location=entry.location_name,
            port=entry.port,
            definitions=str(definitions),
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
        """Env consumed by the code location's scaffolded asset modules.

        ``defs/assets/sql_assets.py`` reads these to reach the sqlmesh-service
        HTTP bridge (see src/templates/defs/assets/sql_assets.py.tpl).
        """
        return {
            "SQLMESH_SERVICE_URL": self._settings.sqlmesh_service_url,
            "DAGSTER_WORKSPACE_PATH": entry.workspace_path,
            # Empty -> sqlmesh-service uses its default gateway.
            "SQLMESH_GATEWAY": "",
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

    def _allocate_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

    @staticmethod
    def _location_name(workspace_path: str) -> str:
        """Derive a stable, GraphQL-safe location name from the workspace path."""
        name = Path(workspace_path).name or "workspace"
        safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
        return f"ws_{safe}"

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
