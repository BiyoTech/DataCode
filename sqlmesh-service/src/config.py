"""Service configuration.

Loads from YAML file (conf/sqlmesh-service.yaml) with environment variable overrides.
Priority: env vars > YAML file > defaults.
"""

import os
from pathlib import Path
from typing import Optional

from agenticdata_common.config.yaml_loader import flatten_yaml_mapping, load_yaml_config
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class Settings(BaseSettings):
    """SQLMesh Service settings."""

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

    # Workspace storage
    workspace_base_path: str = "~/.agenticdata/workspaces"

    # Database
    db_type: str = "sqlite"
    db_sqlite_path: str = ".data/sqlmesh.db"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "agenticdata"
    db_username: str = "root"
    db_password: str = ""
    db_pool_size: int = 5
    db_pool_overflow: int = 10

    # Security
    token_encryption_secret: str = ""  # MUST be set in production

    # Concurrency
    workspace_write_lock_ttl: int = 600
    environment_lock_ttl: int = 900

    # Task (async operations)
    task_timeout_seconds: int = 600
    task_ttl_seconds: int = 3600

    # Logging
    log_level: str = "INFO"

    # Error archive — persist actionable service errors to {workspace}/errors/
    # as JSON files for agent consumption and user diagnostics.
    error_archive_enabled: bool = True
    error_archive_retention: int = 100        # max files per workspace
    error_archive_max_sql_len: int = 4096     # truncate SQL context

    model_config = {"env_prefix": "SQLMESH_SERVICE_"}

    def __init__(self, **kwargs):
        # Load YAML first, then let env vars override via pydantic-settings
        yaml_data = _load_yaml_config("sqlmesh-service.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        merged = {**yaml_flat, **kwargs}
        super().__init__(**merged)


def _flatten_yaml(data: dict, prefix: str = "") -> dict:
    """Flatten nested YAML config to match pydantic field names."""
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
        "workspace.base_path": "workspace_base_path",
        "database.type": "db_type",
        "database.sqlite_path": "db_sqlite_path",
        "database.host": "db_host",
        "database.port": "db_port",
        "database.name": "db_name",
        "database.username": "db_username",
        "database.password": "db_password",
        "database.pool_size": "db_pool_size",
        "database.pool_overflow": "db_pool_overflow",
        "security.token_encryption_secret": "token_encryption_secret",
        "concurrency.workspace_write_lock_ttl": "workspace_write_lock_ttl",
        "concurrency.environment_lock_ttl": "environment_lock_ttl",
        "task.timeout_seconds": "task_timeout_seconds",
        "task.ttl_seconds": "task_ttl_seconds",
        "logging.level": "log_level",
        "error_archive.enabled": "error_archive_enabled",
        "error_archive.retention": "error_archive_retention",
        "error_archive.max_sql_len": "error_archive_max_sql_len",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
