"""Platform-level shared configuration loader.

Reads ``conf/platform.yaml`` (plus an optional ``platform.{env}.yaml`` override
selected by the ``DATACODE_ENV`` environment variable) and exposes typed
accessors for database, workspace, service URLs, and logging.

Each Python service composes this module with its own service-specific
``Settings`` to avoid duplicating cross-cutting configuration.

Usage::

    from datacode_common.config.platform import platform_config

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
from urllib.parse import quote_plus

from pydantic import BaseModel

from datacode_common.config.yaml_loader import load_yaml_config
from datacode_common.database.url import build_database_url

logger = logging.getLogger(__name__)

_PACKAGE_FILE = Path(__file__)


# ── Models ──────────────────────────────────────────────────────────────


class DatabaseServiceConfig(BaseModel):
    """Per-service database override (subset of :class:`DatabaseConfig`)."""

    type: str = "postgresql"       # "sqlite" | "mysql" | "postgresql"
    host: str = "localhost"
    port: int = 5432
    name: str = "datacode"
    username: str = "datacode"
    sqlite_path: str = ".data/datacode.db"
    pool_size: int = 5
    pool_overflow: int = 10


class DatabaseConfig(BaseModel):
    """Database connection shared across services.

    The ``services`` sub-map allows per-service database routing. When a
    service name is present there, :meth:`url` uses the override; otherwise
    it falls back to the top-level fields.

    The password is **always** read from the ``DB_PASSWORD`` environment
    variable — it is never stored in YAML.

    The code defaults describe a local PostgreSQL instance (the platform's
    "PostgreSQL everywhere" decision) and are NOT a supported way to run a
    service: every shipped profile sets these fields explicitly. They exist
    so a process started without a profile fails against a coherent target.
    They must never carry a second dialect's parameters — the previous
    MySQL-era pair (``port=3306``/``username=root``) combined with the base
    file's ``type: postgresql`` into a chimera URL that connected to whatever
    MySQL happened to listen locally.
    """

    type: str = "postgresql"
    host: str = "localhost"
    port: int = 5432
    name: str = "datacode"
    username: str = "datacode"
    pool_size: int = 5
    pool_overflow: int = 10
    sqlite_path: str = ".data/datacode.db"
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


class RedisConfig(BaseModel):
    """Shared coordination backend.

    Redis holds the state that must be identical across replicas: login rate
    limiting, SQLMesh environment locks and the SSE event bus. It is therefore
    read from the platform file rather than per-service files — two services
    disagreeing on the instance or the key prefix would silently stop
    coordinating with each other.

    The password is **always** read from the ``REDIS_PASSWORD`` environment
    variable — it is never stored in YAML.
    """

    enabled: bool = True
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    key_prefix: str = "datacode"
    connect_timeout_seconds: float = 3.0
    operation_timeout_seconds: float = 3.0
    max_connections: int = 32

    @property
    def password(self) -> str:
        """Return the Redis password from the environment (empty when unset)."""
        return os.getenv("REDIS_PASSWORD", "")

    def url(self) -> str:
        """Build a ``redis://`` URL for this instance.

        The password is embedded because redis-py's ``from_url`` is the single
        construction path used across services; callers must therefore treat
        the result as a secret and never log it.
        """
        auth = f":{quote_plus(self.password)}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"

    def safe_url(self) -> str:
        """Return the connection target without credentials, safe to log."""
        return f"redis://{self.host}:{self.port}/{self.db}"

    def key(self, *segments: str) -> str:
        """Join *segments* into a fully namespaced key.

        Namespacing every key under ``{key_prefix}:{cell_id}`` lets one Redis
        instance host several environments (dev/staging) AND several cells
        without collisions. The cell segment is injected here — not at call
        sites — so no domain can accidentally build an un-cell-scoped key.
        """
        # Imported lazily: cell.py must stay importable even if this config
        # module is used by a tool that never configured cells.
        from datacode_common.cell import cell_id

        return ":".join((self.key_prefix, cell_id(), *segments))


class ServiceTopology(BaseModel):
    """Inter-service endpoint map — single source of truth.

    Each attribute is a ``dict[str, str]`` of endpoint keys (e.g.
    ``{"rest_url": "http://localhost:8002"}``).

    Only services that exist are listed. A field absent here makes its
    ``services.<name>`` block in the YAML unreadable — which is how
    ``services.control`` was silently dropped on the Python side while the Go
    loader read it: the profile declared the address, the model discarded it,
    and every consumer fell back to an empty string.
    """

    gateway: dict[str, str] = {}
    agent: dict[str, str] = {}
    # runtime: workspace-runtime, the merged owner of working copies.
    runtime: dict[str, str] = {}
    # control: control-service, the control plane.
    control: dict[str, str] = {}
    # identity: identity-service, the global identity/authz source.
    identity: dict[str, str] = {}
    # egress_proxy / egress_dns: the sandbox egress plane. Only populated
    # by profiles that also deploy the NetworkPolicies making the proxy a
    # sandbox's ONLY outbound path.
    egress_proxy: dict[str, str] = {}
    egress_dns: dict[str, str] = {}

    def url(self, service: str, key: str = "rest_url") -> str:
        """Return the endpoint URL for *service* and *key*.

        Returns an empty string when the service or key is not found.
        """
        svc: dict[str, str] = getattr(self, service, {})
        return svc.get(key, "")


class WorkspaceConfig(BaseModel):
    """Workspace storage configuration."""

    base_path: str = "~/.datacode/workspaces"


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


class CellsConfig(BaseModel):
    """Cell topology declarations (design 03 §2).

    These describe the SHAPE of the installation, not this instance's identity —
    that comes from ``DATACODE_CELL_ID`` (see :mod:`datacode_common.cell`), so a
    single file can be shared by every cell.
    """

    # Whether identity lives in a separate Global database. Declaring it makes a
    # failed split loud: without the declaration, a missing
    # ``database.services.identity`` override silently falls back to the CELL
    # database and every cell ends up sharing one cell's users. See
    # :mod:`datacode_common.db_topology`.
    expect_global_db: bool = False
    # Cell whose stack the global edge should reach when a request carries no
    # placement (a token issued before placement existed, or an unplaced tenant).
    # Not a fallback for a KNOWN-but-unreachable cell: routing a tenant to the
    # wrong cell would show them an empty platform, which is worse than an error.
    default_cell: str = "cell-0"


class PlatformConfig(BaseModel):
    """Top-level shared platform configuration."""

    database: DatabaseConfig = DatabaseConfig()
    redis: RedisConfig = RedisConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()
    services: ServiceTopology = ServiceTopology()
    logging: LoggingConfig = LoggingConfig()
    cells: CellsConfig = CellsConfig()

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

    env = os.getenv("DATACODE_ENV", "")
    if env:
        override = load_yaml_config(
            f"platform.{env}.yaml", package_file=_PACKAGE_FILE
        )
        if override:
            logger.debug("Loaded platform.%s.yaml: %s keys", env, len(override))
            base = _deep_merge(base, override)
        else:
            # A typo in DATACODE_ENV is otherwise indistinguishable from "no
            # profile": the base loads fine and the process runs on defaults.
            logger.warning(
                "config_profile_not_found profile=%s expected_file=platform.%s.yaml "
                "-- running on base defaults, which carry no usable addresses",
                env,
                env,
            )
    else:
        # conf/platform.yaml is deliberately address-free, so without a profile
        # every host/URL falls back to a code default. Those defaults are not
        # wrong so much as arbitrary, and connecting to an arbitrary localhost
        # is the failure this warning exists to make visible. `make doctor`
        # treats the same condition as an error.
        logger.warning(
            "config_profile_unset var=DATACODE_ENV "
            "-- expected one of dev|docker|cell|global|k8s-cell|k8s-global; "
            "platform addresses will fall back to code defaults"
        )

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
