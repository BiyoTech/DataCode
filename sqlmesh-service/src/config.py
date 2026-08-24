"""Service configuration.

Loads from YAML file (conf/sqlmesh-service.yaml) with environment variable overrides.
Priority: env vars > YAML file > defaults.

Shared cross-cutting config (database, workspace, logging, service topology)
is sourced from ``conf/platform.yaml`` via :mod:`datacode_common.config.platform`.
This service-specific YAML only carries settings unique to sqlmesh-service.
"""

import os
from pathlib import Path
from typing import Optional

from datacode_common.config.platform import platform_config
from datacode_common.config.yaml_loader import flatten_yaml_mapping, load_yaml_config
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class Settings(BaseSettings):
    """SQLMesh Service settings.

    Service-specific fields are loaded from ``conf/sqlmesh-service.yaml`` and
    overridable by env vars (prefix ``SQLMESH_SERVICE_``).

    Cross-cutting concerns (database, workspace, logging) are sourced from
    ``conf/platform.yaml`` via the :meth:`platform` property and individual
    properties below. Per-service env vars (e.g. ``SQLMESH_SERVICE_DB_HOST``)
    still take precedence over platform defaults.
    """

    # Server
    grpc_port: int = 50052
    rest_port: int = 8002
    host: str = "0.0.0.0"

    # Context Pool
    context_pool_max_size: int = 20
    context_pool_idle_ttl: int = 1800       # 30 min
    context_pool_max_age: int = 7200        # 2 hr
    context_pool_cleanup_interval: int = 300  # 5 min

    # SQLMesh defaults
    default_dialect: str = "duckdb"
    query_max_row_limit: int = 10000
    query_default_row_limit: int = 1000

    # Security
    token_encryption_secret: str = ""  # MUST be set in production (env-only)

    # Concurrency.
    # The environment lock is deliberately NOT time-bounded: a backfill may
    # legitimately run for hours, and preempting it would allow two concurrent
    # writers on the shared SQLMesh state. It is released when its owning task
    # dies instead (see src.core.env_locks).
    workspace_write_lock_ttl: int = 600

    # Environments (platform registry). Each environment costs a Git branch, a
    # SQLMesh virtual environment and a Dagster deployment, so the live count
    # per workspace is capped and sandboxes always expire.
    max_environments_per_workspace: int = 20
    sandbox_default_ttl_hours: int = 168  # 7 days
    # Reclamation of expired sandboxes. Expiry does not destroy immediately: a
    # warning event is recorded first and the environment survives the grace
    # period, so an owner who is still using it can extend or commit their work.
    environment_reaper_enabled: bool = True
    environment_reaper_interval: int = 3600  # hourly scan
    sandbox_expiry_grace_hours: int = 24

    # Task (async operations)
    task_timeout_seconds: int = 600
    task_ttl_seconds: int = 3600

    # Dagster deployment linkage (create/destroy orchestration + reconcile).
    # Generous by HTTP standards because ensure may spawn a code location
    # subprocess and import the user's asset-dag code before answering.
    dagster_request_timeout: float = 60.0

    # Error archive — persist actionable service errors to {workspace}/errors/
    # as JSON files for agent consumption and user diagnostics.
    error_archive_enabled: bool = True
    error_archive_retention: int = 100        # max files per workspace
    error_archive_max_sql_len: int = 4096     # truncate SQL context

    model_config = {"env_prefix": "SQLMESH_SERVICE_"}

    def __init__(self, **kwargs):
        # Load service-specific YAML first, then let env vars override.
        yaml_data = _load_yaml_config("sqlmesh-service.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        merged = {**yaml_flat, **kwargs}
        super().__init__(**merged)

    # ── Shared platform config (read-only) ────────────────────────────

    @property
    def platform(self):
        """Cached :class:`PlatformConfig` from platform.yaml."""
        return platform_config()

    # ── Database (from platform, with per-service env fallback) ────────

    @property
    def db_type(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_DB_TYPE")
        if env_val:
            return env_val
        return self.platform.database.type

    @property
    def db_sqlite_path(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_DB_SQLITE_PATH")
        if env_val:
            return env_val
        return self.platform.database.sqlite_path

    @property
    def db_host(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_DB_HOST")
        if env_val:
            return env_val
        return self.platform.database.host

    @property
    def db_port(self) -> int:
        env_val = os.getenv("SQLMESH_SERVICE_DB_PORT")
        if env_val:
            return int(env_val)
        return self.platform.database.port

    @property
    def db_name(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_DB_NAME")
        if env_val:
            return env_val
        return self.platform.database.name

    @property
    def db_username(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_DB_USERNAME")
        if env_val:
            return env_val
        return self.platform.database.username

    @property
    def db_password(self) -> str:
        """Database password — env-only (DB_PASSWORD), never from YAML."""
        return os.getenv("DB_PASSWORD", "")

    @property
    def db_pool_size(self) -> int:
        env_val = os.getenv("SQLMESH_SERVICE_DB_POOL_SIZE")
        if env_val:
            return int(env_val)
        return self.platform.database.pool_size

    @property
    def db_pool_overflow(self) -> int:
        env_val = os.getenv("SQLMESH_SERVICE_DB_POOL_OVERFLOW")
        if env_val:
            return int(env_val)
        return self.platform.database.pool_overflow

    @property
    def db_url(self) -> str:
        """Build SQLAlchemy URL from platform config for sqlmesh service."""
        return self.platform.database.url(service="sqlmesh")

    # ── Workspace (from platform) ─────────────────────────────────────

    @property
    def workspace_base_path(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_WORKSPACE_BASE_PATH")
        if env_val:
            return env_val
        return self.platform.workspace_base_path

    # ── Dagster service (from platform) ────────────────────────────────

    @property
    def dagster_service_url(self) -> str:
        """Dagster service URL from platform topology (env override wins).

        Direct service-to-service HTTP for deployment linkage and reconcile —
        no gateway hop, mirroring how dagster-service reaches this service.
        """
        env_val = os.getenv("SQLMESH_SERVICE_DAGSTER_SERVICE_URL")
        if env_val:
            return env_val
        return self.platform.services.url("dagster", "rest_url")

    # ── Logging (from platform) ────────────────────────────────────────

    @property
    def log_level(self) -> str:
        env_val = os.getenv("SQLMESH_SERVICE_LOG_LEVEL")
        if env_val:
            return env_val
        return self.platform.log_level.upper()


def _flatten_yaml(data: dict) -> dict:
    """Flatten service-specific YAML keys to pydantic field names.

    Only sqlmesh-service-specific fields are mapped here. Database, workspace,
    and logging are sourced from platform.yaml (see :class:`Settings` properties).
    """
    mapping = {
        "server.grpc_port": "grpc_port",
        "server.rest_port": "rest_port",
        "server.host": "host",
        "context_pool.max_size": "context_pool_max_size",
        "context_pool.idle_ttl": "context_pool_idle_ttl",
        "context_pool.max_age": "context_pool_max_age",
        "context_pool.cleanup_interval": "context_pool_cleanup_interval",
        "sqlmesh.default_dialect": "default_dialect",
        "sqlmesh.query_max_row_limit": "query_max_row_limit",
        "sqlmesh.query_default_row_limit": "query_default_row_limit",
        "concurrency.workspace_write_lock_ttl": "workspace_write_lock_ttl",
        "environments.max_per_workspace": "max_environments_per_workspace",
        "environments.sandbox_default_ttl_hours": "sandbox_default_ttl_hours",
        "environments.reaper_enabled": "environment_reaper_enabled",
        "environments.reaper_interval": "environment_reaper_interval",
        "environments.sandbox_expiry_grace_hours": "sandbox_expiry_grace_hours",
        "task.timeout_seconds": "task_timeout_seconds",
        "task.ttl_seconds": "task_ttl_seconds",
        "dagster.request_timeout": "dagster_request_timeout",
        "error_archive.enabled": "error_archive_enabled",
        "error_archive.retention": "error_archive_retention",
        "error_archive.max_sql_len": "error_archive_max_sql_len",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
