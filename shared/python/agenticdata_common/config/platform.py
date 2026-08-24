"""Platform-level shared configuration loader.

Reads ``conf/platform.yaml`` (plus an optional ``platform.{env}.yaml`` override
selected by the ``AGENTICDATA_ENV`` environment variable) and exposes typed
accessors for database, workspace, service URLs, and logging.

Each Python service composes this module with its own service-specific
``Settings`` to avoid duplicating cross-cutting configuration.

Usage::

    from agenticdata_common.config.platform import platform_config

    cfg = platform_config()
    db_url = cfg.database.url(service="sqlmesh")
    workspace = cfg.workspace_base_path
    log_level = cfg.log_level
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from agenticdata_common.config.yaml_loader import load_yaml_config
from agenticdata_common.database.url import build_database_url

logger = logging.getLogger(__name__)

_PACKAGE_FILE = Path(__file__)


# ── Models ──────────────────────────────────────────────────────────────


class DatabaseServiceConfig(BaseModel):
    """Per-service database override (subset of :class:`DatabaseConfig`)."""

    type: str = "sqlite"           # "sqlite" | "mysql" | "postgresql"
    host: str = "localhost"
    port: int = 3306
    name: str = "agenticdata"
    username: str = "root"
    sqlite_path: str = ".data/agenticdata.db"
    pool_size: int = 5
    pool_overflow: int = 10


class DatabaseConfig(BaseModel):
    """Database connection shared across services.

    The ``services`` sub-map allows per-service database routing. When a
    service name is present there, :meth:`url` uses the override; otherwise
    it falls back to the top-level fields.

    The password is **always** read from the ``DB_PASSWORD`` environment
    variable — it is never stored in YAML.
    """

    type: str = "sqlite"
    host: str = "localhost"
    port: int = 3306
    name: str = "agenticdata"
    username: str = "root"
    pool_size: int = 5
    pool_overflow: int = 10
    sqlite_path: str = ".data/agenticdata.db"
    services: dict[str, DatabaseServiceConfig] = {}

    def url(self, *, service: Optional[str] = None) -> str:
        """Build a SQLAlchemy async URL for *service* (or the default DB).

        Args:
            service: Service name key from ``database.services``. If ``None``
                or not found, the top-level database config is used.
        """
        svc_cfg: DatabaseConfig | DatabaseServiceConfig = self
        if service and service in self.services:
            svc_cfg = self.services[service]

        password = os.getenv("DB_PASSWORD", "")
        return build_database_url(
            db_type=svc_cfg.type,
            sqlite_path=svc_cfg.sqlite_path,
            host=svc_cfg.host,
            port=svc_cfg.port,
            name=svc_cfg.name,
            username=svc_cfg.username,
            password=password,
            project_root=_PACKAGE_FILE.parent.parent.parent.parent.parent,
        )


class ServiceTopology(BaseModel):
    """Inter-service endpoint map — single source of truth.

    Each attribute is a ``dict[str, str]`` of endpoint keys (e.g.
    ``{"rest_url": "http://localhost:8002"}``).
    """

    gateway: dict[str, str] = {}
    agent: dict[str, str] = {}
    sqlmesh: dict[str, str] = {}
    ontology: dict[str, str] = {}
    dagster: dict[str, str] = {}

    def url(self, service: str, key: str = "rest_url") -> str:
        """Return the endpoint URL for *service* and *key*.

        Returns an empty string when the service or key is not found.
        """
        svc: dict[str, str] = getattr(self, service, {})
        return svc.get(key, "")


class WorkspaceConfig(BaseModel):
    """Workspace storage configuration."""

    base_path: str = "~/.agenticdata/workspaces"


class WorkspaceFileLoggingConfig(BaseModel):
    """Mirroring of workspace-attributable logs into ``{workspace}/logs/``.

    Shared across services on purpose: the on-disk layout is a contract the
    agent and the UI both read, so it must not drift per service.
    """

    enabled: bool = True
    min_level: str = "info"          # debug volume can fill a user's disk
    max_line_bytes: int = 8192
    max_open_files: int = 64
    max_lines_per_sec: int = 200
    retention_days: int = 7


class LoggingConfig(BaseModel):
    """Shared logging configuration."""

    level: str = "info"
    format: str = "json"             # "json" | "text"
    workspace_file: WorkspaceFileLoggingConfig = WorkspaceFileLoggingConfig()


class PlatformConfig(BaseModel):
    """Top-level shared platform configuration."""

    database: DatabaseConfig = DatabaseConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()
    services: ServiceTopology = ServiceTopology()
    logging: LoggingConfig = LoggingConfig()

    # ── Convenience accessors ──────────────────────────────────────────

    @property
    def workspace_base_path(self) -> str:
        """Return the workspace base path (expanded)."""
        return os.path.expanduser(self.workspace.base_path)

    @property
    def log_level(self) -> str:
        """Return the log level string."""
        return self.logging.level

    @property
    def log_format(self) -> str:
        """Return the log format (``json`` or ``text``)."""
        return self.logging.format


# ── Loader ─────────────────────────────────────────────────────────────

_platform: Optional[PlatformConfig] = None


def platform_config() -> PlatformConfig:
    """Return the cached platform config singleton.

    On first call, loads ``platform.yaml`` + optional ``platform.{env}.yaml``
    and merges them (env override wins). Subsequent calls return the cached
    instance.
    """
    global _platform
    if _platform is None:
        _platform = _load_platform()
    return _platform


def _load_platform() -> PlatformConfig:
    """Load and merge platform YAML files, then build :class:`PlatformConfig`."""
    base = load_yaml_config("platform.yaml", package_file=_PACKAGE_FILE)
    logger.debug("Loaded platform.yaml: %s keys", len(base))

    env = os.getenv("AGENTICDATA_ENV", "")
    if env:
        override = load_yaml_config(
            f"platform.{env}.yaml", package_file=_PACKAGE_FILE
        )
        if override:
            logger.debug("Loaded platform.%s.yaml: %s keys", env, len(override))
            base = _deep_merge(base, override)

    # YAML keys whose sub-values are all comments parse as None (e.g.
    # "services:" followed by only commented-out lines). Remove these None
    # values so Pydantic model defaults (e.g. services={}) take effect.
    base = _strip_none_values(base)

    return PlatformConfig(**base)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (override wins).

    Returns a new dict; inputs are not mutated.
    """
    result = copy.deepcopy(base)
    for key, val in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(val, dict)
        ):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _strip_none_values(data: dict[str, Any]) -> dict[str, Any]:
    """Remove keys whose values are ``None`` from nested dicts.

    In YAML, a key followed by only commented-out lines parses as ``None``
    (e.g. ``services:`` with all children commented out). This causes Pydantic
    to reject the value (it expects a dict). Stripping ``None`` values lets
    the model defaults (e.g. ``services={}``) take effect.
    """
    result: dict[str, Any] = {}
    for key, val in data.items():
        if val is None:
            continue
        if isinstance(val, dict):
            result[key] = _strip_none_values(val)
        else:
            result[key] = val
    return result
