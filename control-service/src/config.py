"""Control service configuration.

Loads from YAML file (conf/control-service.yaml) with environment variable
overrides. Priority: env vars > YAML file > defaults.

Shared cross-cutting config (database, workspace, logging, service topology)
is sourced from ``conf/platform.yaml`` via :mod:`datacode_common.config.platform`.
This service-specific YAML only carries settings unique to control-service.
"""

import os
from pathlib import Path

from datacode_common.config.platform import platform_config
from datacode_common.config.yaml_loader import (
    drop_env_provided_keys,
    flatten_yaml_mapping,
    load_yaml_config,
)
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class Settings(BaseSettings):
    """Control service settings.

    Service-specific fields are loaded from ``conf/control-service.yaml`` and
    overridable by env vars (prefix ``CONTROL_SERVICE_``).

    Cross-cutting concerns (database, workspace, logging) are sourced from
    ``conf/platform.yaml`` via the :meth:`platform` property and individual
    properties below.
    """

    # Server
    rest_port: int = 8004
    host: str = "0.0.0.0"

    # Runtime command channel (P5): control-service has NO filesystem access,
    # every Git/SQLMesh-engine side effect is dispatched to workspace-runtime
    # over the internal env-control API, authenticated by this shared token.
    runtime_internal_token: str = ""  # guards calls to /internal/env-control/*, env-only
    # Generous default: scaffold/ensure-worktree run `sqlmesh init` and git.
    runtime_request_timeout: float = 120.0

    # Cell migration channel (P7): guards /internal/cell-migration/* — the
    # export/import/verify/purge surface scripts/cell-migrate.py drives.
    # Fail closed when unset, like every internal surface.
    cell_migration_token: str = ""

    # Security
    token_encryption_secret: str = ""  # MUST be set in production (env-only)

    # Concurrency. The environment lock is deliberately NOT time-bounded: a
    # backfill may legitimately run for hours, and preempting it would allow
    # two concurrent writers on the shared SQLMesh state. It is released when
    # its owning task dies instead (see src.core.env_locks).
    workspace_write_lock_ttl: int = 600

    # Environment lock backend selection: "auto" (default), "redis" or
    # "in_memory". Multi-replica control planes MUST use redis.
    env_lock_backend: str = "auto"
    # Redis lock TTL in seconds. Renewed by a watchdog while the holder lives;
    # it is the recovery bound after a crashed holder stops renewing.
    env_lock_ttl_seconds: int = 120

    # ── Cell capacity (P7) ─────────────────────────────────────────────
    # How many tenants this cell accepts. Published to the global registry at
    # startup and used there to pick the emptiest cell for a new tenant; 0 means
    # "do not place new tenants here", which is how a cell is drained without
    # taking it out of service for the tenants it already holds.
    cell_capacity_tenants: int = 200

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

    # ── Reconciliation: lease & sandbox parties (P8, design 05 §5.1) ────
    # How long a task may be recorded as pending/running with no sandbox Pod
    # before reconciliation calls it a zombie. The audit row is written BEFORE
    # the vehicle exists (so a crashed submission is visible), so every healthy
    # task legitimately looks like this for a few seconds; the window must cover
    # image pull and scheduling on a cold node, which is why it is minutes and
    # not seconds.
    zombie_task_grace_seconds: int = 900

    # Periodic reconciliation sweep. It exists because the lease and sandbox
    # drift kinds are leaks NO user action would ever surface (an abandoned
    # sandbox's stale lease is precisely the one nobody requests again).
    reconcile_sweep_enabled: bool = True
    reconcile_sweep_interval: int = 3600  # hourly, like the reaper
    # Detection is always on; REPAIR starts off. The sweep runs unattended over
    # every workspace and its repairs delete Kubernetes objects and Redis keys,
    # so an operator should first read a few cycles of the drift log and see what
    # it would have done. Flipping this on is the only step needed afterwards.
    reconcile_sweep_repair: bool = False
    # Upper bound per tick. Each workspace costs several round trips, and a cell
    # holding hundreds of them must not spend an entire interval in one sweep;
    # the oldest-activity-first ordering makes a truncated sweep still cover the
    # workspaces least likely to be repaired by ordinary traffic. 0 = no bound.
    reconcile_sweep_max_workspaces: int = 200

    # ── Audit record retention (P8) ─────────────────────────────────────
    # task_records / lease_history are append-only; without expiry both grow
    # monotonically and the reconciler's zombie scan (non-terminal task rows)
    # is the query an unbounded table eventually makes slow. 0 disables one
    # table's retention without disabling the loop.
    retention_enabled: bool = True
    retention_interval: int = 86400  # daily
    task_record_retention_days: int = 90
    lease_history_retention_days: int = 30
    # Upper bound on rows deleted per statement. A first deployment enabling
    # retention after months without it must not run one giant DELETE: that
    # would lock the table and block the writers it serves.
    retention_batch_size: int = 5000

    # ── Tenant quotas (P8) ─────────────────────────────────────────────
    # Platform-wide DEFAULTS applied when a tenant has no tenant_quotas row
    # (the enforcement layer resolves defaults; the schema only stores
    # overrides — see the 0005 migration). Zero is an explicit ban, not "use
    # the default".
    quota_default_max_workspaces: int = 50
    quota_default_max_environments: int = 500
    quota_default_max_concurrent_leases: int = 64
    quota_default_max_runtime_replicas: int = 4
    # Retry-After seconds advertised on 429 quota rejections.
    quota_retry_after_seconds: int = 60
    # TTL of the published quota snapshot in Redis. Refreshed by the capacity
    # loop; sized to survive several missed ticks so a brief control-plane
    # hiccup does not flip enforcement off.
    quota_publish_ttl_seconds: int = 120
    # Fairness weight (design 03 §6): how many of a tenant's leases one runtime
    # instance absorbs before placement starts preferring instances the tenant
    # has not touched. Platform-wide default; 0 disables the fairness term
    # (spread-only placement, the pre-fairness behaviour).
    quota_default_tenant_lease_share: int = 0

    # ── Capacity orchestration (P8 LeaseController) ────────────────────
    # The controller decides from the lease HEARTBEATS (Redis rt:{id}), never
    # from a metrics stack — the observability half of P8 is deferred, and
    # capacity must not depend on it (see the roadmap's P8 scope note).
    capacity_controller_enabled: bool = False
    capacity_controller_interval: int = 30  # seconds between ticks
    # Hysteresis: scale up at/above, consider scaling down at/below. The gap
    # between them is what keeps a steady-state workload from flapping the
    # replica count every tick.
    capacity_scale_up_threshold: float = 0.85
    capacity_scale_down_threshold: float = 0.30
    capacity_min_replicas: int = 1
    capacity_max_replicas: int = 10
    # "log" (default; dev, no cluster SDK) or "kubernetes" (needs the k8s
    # extra) — see src.capacity.drivers.
    capacity_driver: str = "log"
    capacity_k8s_namespace: str = "default"
    capacity_k8s_statefulset: str = "workspace-runtime"
    # A drain that has not finished after this long is expired: the controller
    # scales down anyway and the SIGTERM drain protects the victim's WIP.
    capacity_drain_timeout_seconds: int = 600

    # Dagster deployment linkage (create/destroy orchestration + reconcile).
    # Generous by HTTP standards because ensure may spawn a code location
    # subprocess and import the user's asset-dag code before answering.
    dagster_request_timeout: float = 60.0

    model_config = {"env_prefix": "CONTROL_SERVICE_"}

    def __init__(self, **kwargs):
        # YAML first, then let the environment override it. The env-provided
        # keys are dropped from the init kwargs because pydantic-settings ranks
        # the init source ABOVE the env source: passing them would silently
        # shadow every CONTROL_SERVICE_* override a container sets.
        yaml_data = _load_yaml_config("control-service.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        not_from_env = drop_env_provided_keys(yaml_flat, env_prefix="CONTROL_SERVICE_")
        merged = {**not_from_env, **kwargs}
        super().__init__(**merged)

    # ── Shared platform config (read-only) ────────────────────────────

    @property
    def platform(self):
        """Cached :class:`PlatformConfig` from platform.yaml."""
        return platform_config()

    # ── Database (from platform, with per-service env fallback) ────────

    @property
    def db_type(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_DB_TYPE")
        if env_val:
            return env_val
        return self.platform.database.type

    @property
    def db_sqlite_path(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_DB_SQLITE_PATH")
        if env_val:
            return env_val
        return self.platform.database.sqlite_path

    @property
    def db_host(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_DB_HOST")
        if env_val:
            return env_val
        return self.platform.database.host

    @property
    def db_port(self) -> int:
        env_val = os.getenv("CONTROL_SERVICE_DB_PORT")
        if env_val:
            return int(env_val)
        return self.platform.database.port

    @property
    def db_name(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_DB_NAME")
        if env_val:
            return env_val
        return self.platform.database.name

    @property
    def db_username(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_DB_USERNAME")
        if env_val:
            return env_val
        return self.platform.database.username

    @property
    def db_password(self) -> str:
        """Database password — env-only (DB_PASSWORD), never from YAML."""
        return os.getenv("DB_PASSWORD", "")

    @property
    def db_pool_size(self) -> int:
        env_val = os.getenv("CONTROL_SERVICE_DB_POOL_SIZE")
        if env_val:
            return int(env_val)
        return self.platform.database.pool_size

    @property
    def db_pool_overflow(self) -> int:
        env_val = os.getenv("CONTROL_SERVICE_DB_POOL_OVERFLOW")
        if env_val:
            return int(env_val)
        return self.platform.database.pool_overflow

    @property
    def db_url(self) -> str:
        """Build SQLAlchemy URL from platform config for the control service.

        Reads the ``control`` entry of ``database.services`` when present and
        falls back to the top-level database fields otherwise — which is the
        normal single-database deployment. The control plane and the runtime
        share one database (the runtime reads the same environment rows), so a
        per-service override would have to be applied to both.
        """
        return self.platform.database.url(service="control")

    # ── Workspace (from platform) ─────────────────────────────────────
    # NOTE: used ONLY to derive paths handed to workspace-runtime — the
    # control plane itself never touches this directory.

    @property
    def workspace_base_path(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_WORKSPACE_BASE_PATH")
        if env_val:
            return env_val
        return self.platform.workspace_base_path

    # ── Runtime service (from platform) ────────────────────────────────

    @property
    def runtime_rest_url(self) -> str:
        """Base URL of workspace-runtime, the owner of every working copy.

        The binders dispatch Git materialization, SQLMesh virtual environments,
        workspace provisioning AND the Dagster data plane here (design 06 P5;
        P3 merged the data plane into the runtime).
        """
        env_val = os.getenv("CONTROL_SERVICE_RUNTIME_REST_URL")
        if env_val:
            return env_val
        return self.platform.services.url("runtime", "rest_url") or ""

    # ── Redis (from platform) ─────────────────────────────────────────

    @property
    def redis_url(self) -> str:
        """Redis coordination backend URL (environment locks, P5 shared state)."""
        env_val = os.getenv("CONTROL_SERVICE_REDIS_URL")
        if env_val:
            return env_val
        return getattr(self.platform, "redis_url", "")

    # ── Logging (from platform) ────────────────────────────────────────

    @property
    def log_level(self) -> str:
        env_val = os.getenv("CONTROL_SERVICE_LOG_LEVEL")
        if env_val:
            return env_val
        return self.platform.log_level.upper()


def _flatten_yaml(data: dict) -> dict:
    """Flatten service-specific YAML keys to pydantic field names.

    Only control-service-specific fields are mapped here. Database, workspace,
    and logging are sourced from platform.yaml (see :class:`Settings` properties).
    """
    mapping = {
        "server.rest_port": "rest_port",
        "server.host": "host",
        "runtime.internal_token": "runtime_internal_token",
        "runtime.request_timeout": "runtime_request_timeout",
        "concurrency.workspace_write_lock_ttl": "workspace_write_lock_ttl",
        "concurrency.env_lock_backend": "env_lock_backend",
        "concurrency.env_lock_ttl_seconds": "env_lock_ttl_seconds",
        "cell.capacity_tenants": "cell_capacity_tenants",
        "environments.max_per_workspace": "max_environments_per_workspace",
        "environments.sandbox_default_ttl_hours": "sandbox_default_ttl_hours",
        "environments.reaper_enabled": "environment_reaper_enabled",
        "environments.reaper_interval": "environment_reaper_interval",
        "environments.sandbox_expiry_grace_hours": "sandbox_expiry_grace_hours",
        "reconcile.zombie_task_grace_seconds": "zombie_task_grace_seconds",
        "reconcile.sweep_enabled": "reconcile_sweep_enabled",
        "reconcile.sweep_interval": "reconcile_sweep_interval",
        "reconcile.sweep_repair": "reconcile_sweep_repair",
        "reconcile.sweep_max_workspaces": "reconcile_sweep_max_workspaces",
        "retention.enabled": "retention_enabled",
        "retention.interval": "retention_interval",
        "retention.task_record_days": "task_record_retention_days",
        "retention.lease_history_days": "lease_history_retention_days",
        "retention.batch_size": "retention_batch_size",
        "dagster.request_timeout": "dagster_request_timeout",
        "quota.defaults.max_workspaces": "quota_default_max_workspaces",
        "quota.defaults.max_environments": "quota_default_max_environments",
        "quota.defaults.max_concurrent_leases": "quota_default_max_concurrent_leases",
        "quota.defaults.max_runtime_replicas": "quota_default_max_runtime_replicas",
        "quota.retry_after_seconds": "quota_retry_after_seconds",
        "quota.publish_ttl_seconds": "quota_publish_ttl_seconds",
        "quota.defaults.tenant_lease_share": "quota_default_tenant_lease_share",
        "capacity.controller_enabled": "capacity_controller_enabled",
        "capacity.controller_interval": "capacity_controller_interval",
        "capacity.scale_up_threshold": "capacity_scale_up_threshold",
        "capacity.scale_down_threshold": "capacity_scale_down_threshold",
        "capacity.min_replicas": "capacity_min_replicas",
        "capacity.max_replicas": "capacity_max_replicas",
        "capacity.driver": "capacity_driver",
        "capacity.k8s_namespace": "capacity_k8s_namespace",
        "capacity.k8s_statefulset": "capacity_k8s_statefulset",
        "capacity.drain_timeout_seconds": "capacity_drain_timeout_seconds",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
