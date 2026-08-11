"""Service configuration.

Loads from YAML file (conf/dagster-service.yaml) with environment variable
overrides. Priority: env vars > YAML file > defaults.

Key design insight (see package docstring): the Code Location gRPC server is
ALWAYS local because it loads user Python code from ``asset-dag/``. Only the
DagsterInstance (daemon + webserver + metadata DB) can be remote.
"""

from pathlib import Path

from agenticdata_common.config.yaml_loader import flatten_yaml_mapping, load_yaml_config
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class Settings(BaseSettings):
    """Dagster Service settings — supports ``local`` and ``remote`` modes."""

    # Server. NOTE: 8003 is ontology-service, so dagster-service uses 8004.
    rest_port: int = 8004
    host: str = "0.0.0.0"

    # Dagster mode: "local" (full local stack) or "remote" (remote DagsterInstance).
    dagster_mode: str = "local"

    # Code Location gRPC config — ALWAYS local in both modes.
    code_location_host: str = "0.0.0.0"
    code_location_port: int = 4000

    # Local mode only: DagsterInstance + daemon + webserver.
    dagster_home: str = "~/.agenticdata/dagster_home"
    dagster_daemon_enabled: bool = True
    dagster_webserver_enabled: bool = True
    dagster_webserver_host: str = "0.0.0.0"
    dagster_webserver_port: int = 3001
    # GraphQL URL used by dagster_client in local mode.
    dagster_graphql_url: str = "http://localhost:3001/graphql"

    # Remote mode only: connect to a remote DagsterInstance webserver.
    dagster_remote_graphql_url: str = ""  # e.g. "http://dagster-prod:3000/graphql"
    dagster_remote_token: str = ""        # Auth token for remote GraphQL (env-only)

    # Subprocess lifecycle.
    process_start_timeout: int = 30       # seconds to wait for a subprocess to become healthy
    process_stop_timeout: int = 5         # seconds to wait for graceful SIGTERM before SIGKILL

    # Workspace storage — must match sqlmesh-service base path.
    workspace_base_path: str = "~/.agenticdata/workspaces"

    # SQLMesh service URL (for the SQLMesh -> Dagster bridge).
    sqlmesh_service_url: str = "http://localhost:8002"
    sqlmesh_request_timeout: float = 10.0

    # Logging.
    log_level: str = "INFO"

    model_config = {"env_prefix": "DAGSTER_SERVICE_"}

    def __init__(self, **kwargs):
        # Load YAML first, then let env vars override via pydantic-settings.
        yaml_data = _load_yaml_config("dagster-service.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        merged = {**yaml_flat, **kwargs}
        super().__init__(**merged)

    @property
    def is_remote(self) -> bool:
        """True when connecting to a remote DagsterInstance."""
        return self.dagster_mode.lower() == "remote"

    @property
    def active_graphql_url(self) -> str:
        """GraphQL endpoint used by the client for the active mode."""
        return self.dagster_remote_graphql_url if self.is_remote else self.dagster_graphql_url


def _flatten_yaml(data: dict) -> dict:
    """Flatten nested YAML config to match pydantic field names."""
    mapping = {
        "server.rest_port": "rest_port",
        "server.host": "host",
        "dagster.mode": "dagster_mode",
        "dagster.code_location_host": "code_location_host",
        "dagster.code_location_port": "code_location_port",
        "dagster.home": "dagster_home",
        "dagster.daemon_enabled": "dagster_daemon_enabled",
        "dagster.webserver_enabled": "dagster_webserver_enabled",
        "dagster.webserver_host": "dagster_webserver_host",
        "dagster.webserver_port": "dagster_webserver_port",
        "dagster.graphql_url": "dagster_graphql_url",
        "dagster.remote_graphql_url": "dagster_remote_graphql_url",
        "dagster.remote_token": "dagster_remote_token",
        "dagster.process_start_timeout": "process_start_timeout",
        "dagster.process_stop_timeout": "process_stop_timeout",
        "workspace.base_path": "workspace_base_path",
        "sqlmesh.service_url": "sqlmesh_service_url",
        "sqlmesh.request_timeout": "sqlmesh_request_timeout",
        "logging.level": "log_level",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
