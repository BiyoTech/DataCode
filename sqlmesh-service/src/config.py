"""Service configuration.

Loads from YAML file (conf/sqlmesh-service.yaml) with environment variable overrides.
Priority: env vars > YAML file > defaults.

Shared cross-cutting config (database, workspace, logging, service topology)
is sourced from ``conf/platform.yaml`` via :mod:`agenticdata_common.config.platform`.
This service-specific YAML only carries settings unique to sqlmesh-service.
"""

import os
from pathlib import Path
from typing import Optional

from agenticdata_common.config.platform import platform_config
from agenticdata_common.config.yaml_loader import flatten_yaml_mapping, load_yaml_config
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

    # Concurrency
    workspace_write_lock_ttl: int = 600
    environment_lock_ttl: int = 900

    # Task (async operations)
    task_timeout_seconds: int = 600
    task_ttl_seconds: int = 3600

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
        "concurrency.environment_lock_ttl": "environment_lock_ttl",
        "task.timeout_seconds": "task_timeout_seconds",
        "task.ttl_seconds": "task_ttl_seconds",
        "error_archive.enabled": "error_archive_enabled",
        "error_archive.retention": "error_archive_retention",
        "error_archive.max_sql_len": "error_archive_max_sql_len",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
