"""workspace-runtime configuration — the merged settings of the former
sqlmesh-service and dagster-service (design 06, P3).

Loads from ``conf/workspace-runtime.yaml`` with environment variable
overrides. Priority: env vars > YAML file > defaults. Shared cross-cutting
config (database, workspace, logging) still comes from ``conf/platform.yaml``
via :mod:`datacode_common.config.platform`.

Port note: the host services occupy 8000-8004 contiguously — 8000 gateway,
8001 agent, 8002 workspace-runtime (this service), 8003 identity, 8004 control.
"""

from __future__ import annotations

import os
from pathlib import Path

from datacode_common.config.platform import platform_config
from datacode_common.config.yaml_loader import (
    drop_env_provided_keys,
    flatten_yaml_mapping,
    load_yaml_config,
)
from pydantic import model_validator
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)

# Bind addresses that carry no routing information and must never be advertised
# to the control plane.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class Settings(BaseSettings):
    """workspace-runtime settings (sqlmesh engine + dagster data plane +
    ontology + executor)."""

    # ── Server ────────────────────────────────────────────────────────
    rest_port: int = 8002
    host: str = "0.0.0.0"

    # ── SQLMesh metadata (P6 close-out: reads go through the sandbox) ──
    # TTL of the runtime's cached metadata payloads (model lists, metrics,
    # environments, lineage). Explicit dirty/invalidate calls bypass it; the
    # TTL is the backstop for edits made outside our API.
    metadata_cache_ttl_seconds: float = 60.0

    sqlmesh_default_dialect: str = "duckdb"
    query_max_row_limit: int = 10000
    query_default_row_limit: int = 1000

    token_encryption_secret: str = ""   # env-only in production

    workspace_write_lock_ttl: int = 600
    env_lock_backend: str = "auto"      # auto | redis | in_memory
    env_lock_ttl_seconds: int = 120

    max_environments_per_workspace: int = 20
    sandbox_default_ttl_hours: int = 168
    environment_reaper_enabled: bool = True
    environment_reaper_interval: int = 3600
    sandbox_expiry_grace_hours: int = 24

    task_timeout_seconds: int = 600
    task_ttl_seconds: int = 3600

    error_archive_enabled: bool = True
    error_archive_retention: int = 100
    error_archive_max_sql_len: int = 4096

    # ── Dagster data plane (from dagster-service) ─────────────────────
    dagster_mode: str = "local"         # local | remote
    # Vehicle that runs a code location's user code:
    #   process — a child of workspace-runtime (dev / single-node). Shares this
    #             process's filesystem view and network namespace.
    #   pod     — one Pod per location on the user-code pool (production, P6).
    #             Requires a container isolation profile; the manager refuses to
    #             start rather than silently degrading to the process vehicle.
    code_location_mode: str = "process"
    code_location_container_image: str = "datacode/task-runner:latest"
    code_location_pod_ready_timeout: int = 120
    code_location_host: str = "0.0.0.0"
    code_location_port: int = 4000
    code_location_port_segment_size: int = 50
    code_location_port_max_segments: int = 64
    code_location_advertise_host: str = ""

    internal_token: str = ""            # guards /internal/*, env-only

    dagster_home: str = "~/.datacode/dagster_home"
    dagster_daemon_enabled: bool = True
    dagster_webserver_enabled: bool = True
    dagster_webserver_host: str = "0.0.0.0"
    dagster_webserver_port: int = 3001
    dagster_graphql_url: str = "http://localhost:3001/graphql"
    dagster_remote_graphql_url: str = ""
    dagster_remote_token: str = ""

    process_start_timeout: int = 30
    process_stop_timeout: int = 5
    process_health_interval: int = 10
    process_max_restarts: int = 3
    process_restart_window: int = 300

    sqlmesh_request_timeout: float = 10.0

    # ── L3-proc executor (design 04 §2.3, P3e) ────────────────────────
    # The executable SQLMesh Context lives in a PERSISTENT CHILD PROCESS,
    # never in this service's process: constructing it execs the user's
    # Python models and macros, which stay supported (Q1). One executor per
    # working copy keeps the Context hot, so isolation costs one UDS round
    # trip instead of a Context rebuild.
    executor_enabled: bool = False      # P3 ships the mechanism; flip on per env
    # Vehicle for the warm sandbox (P6c). "process" = the L3-proc child (dev,
    # single node); "container" = an L3-base Pod per (working copy, gateway),
    # speaking the SAME frozen protocol over TCP. This is the switch the P0
    # freeze was designed for: callers are untouched by the swap, and rolling
    # back is setting it back to "process".
    executor_mode: str = "process"      # "process" | "container"
    # Image for executor_mode=container: the workspace-runtime package plus the
    # engine, running `python -m src.executor.server --tcp`.
    executor_container_image: str = "datacode/workspace-runtime:latest"
    # Seconds a sandbox Pod may take from creation to a routable address.
    executor_container_ready_timeout: int = 120
    executor_user_base: int = 60000     # distinct unprivileged uid per slot
    executor_socket_dir: str = "/run/datacode/executors"
    executor_idle_stop_seconds: int = 900
    executor_max_restarts: int = 3
    executor_restart_window_seconds: int = 300
    executor_address_space_bytes: int = 4294967296   # RLIMIT_AS, 4 GiB
    executor_cpu_seconds: int = 1800                 # RLIMIT_CPU
    executor_max_processes: int = 256                # RLIMIT_NPROC
    executor_max_file_size_bytes: int = 2147483648   # RLIMIT_FSIZE
    executor_max_open_files: int = 1024              # RLIMIT_NOFILE
    executor_shell_timeout_seconds: int = 120

    # ── Task execution plane (design 04 §3, design 06 P6a) ─────────────
    # Where a task that must NOT run in this process is sent. The isolation
    # profile decides WHICH level a task kind needs; this decides whether that
    # level is available at all (see src/isolation/policy.py):
    #
    #   disabled    — no external vehicle; container profiles are unavailable
    #                 and container-level task kinds are refused.
    #   local       — one-shot hardened child process running task-runner.
    #                 The dev / single-node vehicle: same spec-in/result-out
    #                 contract as Kubernetes, without the container boundary.
    #   kubernetes  — one Job per task (production). The ONLY backend that can
    #                 serve L2/L3-base, because those levels ARE the container.
    task_backend: str = "disabled"
    # Root of the task-runner package (holds src/main.py). Empty resolves to
    # the sibling `task-runner/` directory of this repository checkout, which is
    # where the local backend finds it in development.
    task_runner_root: str = ""
    # Ceiling applied on top of the profile's own timeout. A profile may allow
    # an hour; a deployment may still want a shorter maximum.
    task_max_timeout_seconds: int = 3600
    # Platform-owned root for artefacts a sandbox reads but must not be able to
    # rewrite — today the generated command guards (src/isolation/guards.py).
    # Deliberately NOT inside a working copy: every copy is writable by the task
    # that mounts it, so a guard placed there could be replaced by the code it
    # guards against. Empty resolves to ~/.datacode/sandbox.
    sandbox_tool_root: str = ""

    # ── Warm pool (P6c): resident task runners ────────────────────────
    # Off by default: it only works with the kubernetes backend, and a resident
    # runner is a long-lived vehicle whose value shows up under real traffic.
    warm_pool_enabled: bool = False
    # Keyed by WORKING COPY (not tenant) to keep the single-copy subPath mount —
    # see src/tasks/warm_pool.py for why the roadmap's per-tenant sketch cannot
    # be built without giving up the T1 closure.
    warm_pool_max_pods: int = 8
    # Retire after this many tasks: a task can damage its own interpreter (a
    # leaked thread, a segfaulting extension) and the next one would inherit it.
    warm_pool_max_tasks: int = 50
    warm_pool_idle_seconds: int = 900
    warm_pool_ready_timeout: int = 120
    # Kinds allowed on a resident runner. Read-only by construction: the L2
    # profile carries a writable warehouse account, and a Pod keeping one alive
    # between tasks is the coexistence Q5 forbids.
    warm_pool_kinds: tuple[str, ...] = ("agent.shell",)
    # Bytes of runner output retained for diagnostics. The RESULT is a single
    # structured line and is never truncated away; this bounds the surrounding
    # engine chatter that is kept with a failure.
    task_log_tail_bytes: int = 65536

    # ── Working-copy Python dependencies ──────────────────────────────
    # The package index a `deps.sync` task may reach. PLATFORM configuration on
    # purpose: the sandbox's egress allow-list is otherwise derived from the
    # working copy, and an index named by a file in the repository would be a
    # way to widen that allow-list from inside the code under review.
    #
    # Empty means dependency installation is NOT AVAILABLE. Refusing is the only
    # honest default: falling back to the public index would mean a deployment
    # that never configured an index still reaches out to the internet from a
    # profile whose whole purpose is a narrow allow-list.
    deps_index_url: str = ""
    # Additional indexes, comma-separated. Same provenance rule.
    deps_extra_index_urls: str = ""
    # Hosts pip may use over plain HTTP (`--trusted-host`), comma-separated. For
    # an internal mirror without a public certificate.
    deps_trusted_hosts: str = ""
    # Size ceiling for one working copy's overlay tree. The target is the user's
    # own volume, and a transitive CUDA build is tens of gigabytes: without a cap
    # a single declaration can fill the copy and take the lease down with it.
    deps_max_bytes: int = 2 * 1024 * 1024 * 1024
    # Budget for one install. Below the profile's own timeout so the runner
    # reports a classified failure instead of being killed by the vehicle.
    deps_install_timeout_seconds: int = 840
    # Baseline pins that may NEVER be yielded to a working copy, beyond the ones
    # the platform derives itself (its own runtime, and everything shipping a
    # compiled extension). Comma-separated canonical names. For a deployment
    # whose assets depend on a specific pure-Python version the platform also
    # uses — an authentication client, a tracing SDK — where an override would
    # be silent and confusing rather than local.
    deps_protected_packages: str = ""
    # The opposite, and applied LAST so it wins: pins this deployment allows a
    # working copy to override even though the rules above withhold them.
    # Comma-separated canonical names. The escape hatch a failed install names
    # in its message, so an operator who has decided a specific package is safe
    # to override does not need a platform release to say so.
    deps_relaxable_packages: str = ""

    # ── Kubernetes task backend ────────────────────────────────────────
    # Namespace the Jobs are created in. Must be the namespace whose
    # NetworkPolicy denies egress by default (P6b).
    task_namespace: str = "datacode-tasks"
    task_runner_image: str = "datacode/task-runner:latest"
    task_runner_image_pull_policy: str = "IfNotPresent"
    # Zero-RBAC service account. A Job must not be able to read Secrets or
    # create Pods, so this account is expected to have NO role bindings.
    task_service_account: str = "task-runner-noperm"
    # This pod's node, injected via the downward API (spec.nodeName). The S1
    # working-copy hand-off requires the runner to land on the SAME node that
    # holds the lease, because that is where the working copy exists.
    task_node_name: str = ""
    # Node-level directory holding working copies, when the medium exposes one
    # (mount_kind: ephemeral/hostPath). Empty means the hostPath strategy is
    # unavailable and the deployment must supply a shared PVC instead.
    task_host_workdir: str = ""
    # Per-replica PVC holding the working copies (mount_kind: instance_local).
    # ReadWriteOnce permits several pods on ONE node, which is why the runner
    # must be co-scheduled with this replica.
    task_workcopy_pvc: str = ""
    # Seconds a finished Job is kept before Kubernetes reaps it. Long enough to
    # read its logs after a failure, short enough not to accumulate.
    task_job_ttl_seconds: int = 300
    # Address of the controlled DNS resolver, used for profiles declaring
    # `dns_policy: None`. Empty downgrades those Pods to ClusterFirst with a
    # warning: a Pod with dnsPolicy None and no dnsConfig cannot resolve anything
    # at all, which would look like a network bug rather than a missing setting.
    task_dns_server: str = ""

    # ── Sandbox network and credentials (design 04 §3.4-3.5, P6b) ───────
    # Address of the egress proxy, the ONLY outbound path open to a sandbox.
    # Empty means no proxy is deployed: sandboxes then get no proxy variables and,
    # under the NetworkPolicy, no egress at all. That is the correct default —
    # a platform without the proxy must not grant user code direct network access.
    egress_proxy_url: str = ""
    # Byte budget per task, both directions combined. 0 takes the shared default
    # from `datacode_common.egress_grant`, which is a real number and never
    # "unlimited": an unbounded transfer is how exfiltration stays invisible.
    egress_byte_quota: int = 0
    # Permit the in-process egress grant store when Redis is unreachable.
    # DEVELOPMENT ONLY: the runtime and the proxy are separate processes in
    # production, so a local store would leave the proxy with no grant at all.
    egress_allow_local_grants: bool = False
    # Refuse to submit a task when its egress grant cannot be published. On by
    # default: launching a sandbox whose network policy was never delivered
    # produces a task that fails for a reason nobody can trace.
    egress_require_grant: bool = True

    # ── Working-copy storage contract (design 07 ADR-003 / Q7, P4) ─────
    # There is exactly ONE materialization code path; the medium behind
    # materialize.root is a deployment concern. `mount_kind` DECLARES what
    # that medium is so the process can refuse a combination that loses data
    # silently (see datacode_common.materialize for the full matrix).
    #
    # Defaults describe the CURRENT pre-P4 deployment: the shared volume that
    # sqlmesh-service used, with no lease routing yet. P4b flips these to
    # instance_local + lease_enabled.
    materialize_root: str = ""          # defaults to workspace_base_path
    materialize_mount_kind: str = "shared"
    # LRU reclamation (design 06 4b): when the materialize volume crosses the
    # HIGH watermark, working copies with no lease are discarded (worktree
    # only; .repo is kept) until usage is back at/below the LOW watermark.
    # Only meaningful on replica-private media (instance_local) — on a shared
    # volume one replica cannot reclaim another's copies.
    materialize_disk_high_watermark_percent: int = 85
    materialize_disk_low_watermark_percent: int = 70
    materialize_reclaim_interval_seconds: int = 300
    lease_enabled: bool = False
    # TTL is a CRASH-RECOVERY BOUND, not an operation timeout: a watchdog
    # renews every ttl/3 while the holder lives, so a long apply never loses
    # its lease. See datacode_common.lease.
    lease_ttl_seconds: int = 120
    # Hard cap on working copies served by ONE instance. 0 = derive capacity
    # from the memory budget alone (design 07 Q3).
    lease_max_per_instance: int = 16
    # The real admission driver: refuse new leases past this fraction of the
    # pod's measured RSS even when the count limit is not reached, because the
    # per-lease estimate scales with the user's repository size.
    lease_admission_rss_high_watermark: float = 0.80
    lease_idle_release_seconds: int = 1800
    # Retry-After seconds advertised on 429 tenant-quota rejections from the
    # acquire path (P8). The limits themselves live in control-service and are
    # read from Redis; only the retry hint is runtime-local.
    lease_quota_retry_after_seconds: int = 60
    # Bounds how much uncommitted work a NON-GRACEFUL crash can lose. This is a
    # user-facing durability contract (design 07 Q6), NOT a tuning knob:
    # changing it changes what the product promises. Shortening it costs
    # Git-server write amplification (one stash + hidden-ref push each).
    wip_snapshot_interval_seconds: int = 600
    # Idle leases cost nothing when their working copy is clean.
    wip_snapshot_skip_if_clean: bool = True

    # ── Platform-derived settings (resolved once, explicit value wins) ──
    workspace_base_path: str = ""
    # Self-address handed to code locations and Dagster runs so their SQLMesh
    # calls come back to this runtime (P3 merged the engine in; the standalone
    # sqlmesh-service is gone). Resolved from services.runtime.
    sqlmesh_service_url: str = ""
    log_level: str = ""

    model_config = {"env_prefix": "WORKSPACE_RUNTIME_"}

    def __init__(self, **kwargs):
        # YAML first, then let the environment override it. The env-provided
        # keys are dropped from the init kwargs because pydantic-settings ranks
        # the init source ABOVE the env source: passing them would silently
        # shadow every WORKSPACE_RUNTIME_* override a container sets.
        yaml_data = _load_yaml_config("workspace-runtime.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        not_from_env = drop_env_provided_keys(
            yaml_flat, env_prefix="WORKSPACE_RUNTIME_"
        )
        merged = {**not_from_env, **kwargs}
        super().__init__(**merged)

    @model_validator(mode="after")
    def _inherit_platform_defaults(self) -> "Settings":
        """Fill unset cross-cutting settings from ``conf/platform.yaml``."""
        platform = platform_config()
        if not self.workspace_base_path:
            self.workspace_base_path = platform.workspace_base_path
        if not self.materialize_root:
            # Until P4b splits them, working copies are materialized into the
            # same tree the platform already declares.
            self.materialize_root = self.workspace_base_path
        if not self.sqlmesh_service_url:
            # services.runtime, NOT the retired services.sqlmesh: the SQLMesh
            # engine lives in this process, so code locations and Dagster runs
            # must call back here.
            self.sqlmesh_service_url = platform.services.url("runtime", "rest_url")
        self.log_level = (self.log_level or platform.log_level).upper()
        return self

    # ── Working-copy storage contract ─────────────────────────────────

    def storage_contract(self):
        """Validate medium vs. lease/lock guarantees, or raise.

        Called during startup so an unsafe deployment fails loudly instead of
        corrupting working copies. ``distributed_env_lock`` reflects the env
        lock backend: only a distributed one can serialize writes arriving at
        different replicas, which is what a shared medium depends on.
        """
        from datacode_common.materialize import validate_storage_contract

        return validate_storage_contract(
            root=self.materialize_root,
            mount_kind=self.materialize_mount_kind,
            lease_enabled=self.lease_enabled,
            distributed_env_lock=self.env_lock_backend.strip().lower()
            in ("auto", "redis"),
        )

    # ── Shared platform config (read-only) ────────────────────────────

    @property
    def platform(self):
        return platform_config()

    # ── Database (from platform, per-service env fallback) ────────────
    # The moved sqlmesh/workspace modules read these snake-case accessors;
    # they resolve exactly like the legacy sqlmesh-service ones did.

    @property
    def db_type(self) -> str:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_TYPE")
        return env_val or self.platform.database.type

    @property
    def db_sqlite_path(self) -> str:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_SQLITE_PATH")
        return env_val or self.platform.database.sqlite_path

    @property
    def db_host(self) -> str:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_HOST")
        return env_val or self.platform.database.host

    @property
    def db_port(self) -> int:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_PORT")
        return int(env_val) if env_val else self.platform.database.port

    @property
    def db_name(self) -> str:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_NAME")
        return env_val or self.platform.database.name

    @property
    def db_username(self) -> str:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_USERNAME")
        return env_val or self.platform.database.username

    @property
    def db_password(self) -> str:
        """Database password — env-only (DB_PASSWORD), never from YAML."""
        return os.getenv("DB_PASSWORD", "")

    @property
    def db_pool_size(self) -> int:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_POOL_SIZE")
        return int(env_val) if env_val else self.platform.database.pool_size

    @property
    def db_pool_overflow(self) -> int:
        env_val = os.getenv("WORKSPACE_RUNTIME_DB_POOL_OVERFLOW")
        return int(env_val) if env_val else self.platform.database.pool_overflow

    @property
    def db_url(self) -> str:
        """Metadata DB URL — the SAME database control-service owns.

        Both services must resolve to one database: control-service owns the
        registry tables and the runtime reads them (plus its own drafts/WIP
        state). They therefore share the ``control`` key of
        ``database.services`` — using different keys would let a per-service
        override silently split the two halves of one dataset.
        """
        env_val = os.getenv("WORKSPACE_RUNTIME_DATABASE_URL")
        if env_val:
            return env_val
        return self.platform.database.url(service="control")

    # ── Dagster mode helpers (from dagster-service) ───────────────────

    @property
    def is_remote(self) -> bool:
        return self.dagster_mode.lower() == "remote"

    @property
    def active_graphql_url(self) -> str:
        return self.dagster_remote_graphql_url if self.is_remote else self.dagster_graphql_url

    @property
    def internal_api_enabled(self) -> bool:
        """Whether /internal/* may serve requests. Fail closed without the
        shared secret: an unauthenticated endpoint listing this replica's
        gRPC endpoints is a map of the data plane."""
        return bool(self.internal_token)

    @property
    def advertised_code_location_host(self) -> str:
        """Host the control plane should dial, or empty when not advertisable.

        ``code_location_host`` is a BIND address and only usable as an
        advertised one when it is a concrete interface; a wildcard bind says
        nothing about how to reach this replica.
        """
        if self.code_location_advertise_host:
            return self.code_location_advertise_host
        if self.code_location_host in _WILDCARD_HOSTS:
            return ""
        return self.code_location_host


def _flatten_yaml(data: dict) -> dict:
    """Flatten workspace-runtime YAML keys to pydantic field names."""
    mapping = {
        "server.rest_port": "rest_port",
        "server.host": "host",
        "sqlmesh.metadata_cache_ttl_seconds": "metadata_cache_ttl_seconds",
        "sqlmesh.default_dialect": "sqlmesh_default_dialect",
        "sqlmesh.query_max_row_limit": "query_max_row_limit",
        "sqlmesh.query_default_row_limit": "query_default_row_limit",
        "concurrency.workspace_write_lock_ttl": "workspace_write_lock_ttl",
        "concurrency.env_lock_backend": "env_lock_backend",
        "concurrency.env_lock_ttl_seconds": "env_lock_ttl_seconds",
        "environments.max_per_workspace": "max_environments_per_workspace",
        "environments.sandbox_default_ttl_hours": "sandbox_default_ttl_hours",
        "environments.reaper_enabled": "environment_reaper_enabled",
        "environments.reaper_interval": "environment_reaper_interval",
        "environments.sandbox_expiry_grace_hours": "sandbox_expiry_grace_hours",
        "task.timeout_seconds": "task_timeout_seconds",
        "task.ttl_seconds": "task_ttl_seconds",
        # Execution plane (P6a). These were reachable through the environment
        # only, so `task.backend` in the YAML was silently ignored and every
        # deployment ran with the built-in default (`disabled`) — which refuses
        # every sandboxed task, including `agent.shell`.
        "task.backend": "task_backend",
        "task.runner_root": "task_runner_root",
        "task.max_timeout_seconds": "task_max_timeout_seconds",
        "task.sandbox_tool_root": "sandbox_tool_root",
        "task.log_tail_bytes": "task_log_tail_bytes",
        "task.namespace": "task_namespace",
        "task.runner_image": "task_runner_image",
        "task.runner_image_pull_policy": "task_runner_image_pull_policy",
        "task.service_account": "task_service_account",
        "task.host_workdir": "task_host_workdir",
        "task.workcopy_pvc": "task_workcopy_pvc",
        "task.job_ttl_seconds": "task_job_ttl_seconds",
        "task.dns_server": "task_dns_server",
        "deps.index_url": "deps_index_url",
        "deps.extra_index_urls": "deps_extra_index_urls",
        "deps.trusted_hosts": "deps_trusted_hosts",
        "deps.max_bytes": "deps_max_bytes",
        "deps.install_timeout_seconds": "deps_install_timeout_seconds",
        "deps.protected_packages": "deps_protected_packages",
        "deps.relaxable_packages": "deps_relaxable_packages",
        "error_archive.enabled": "error_archive_enabled",
        "error_archive.retention": "error_archive_retention",
        "error_archive.max_sql_len": "error_archive_max_sql_len",
        "dagster.mode": "dagster_mode",
        "dagster.code_location_mode": "code_location_mode",
        "dagster.code_location_container_image": "code_location_container_image",
        "dagster.code_location_pod_ready_timeout": "code_location_pod_ready_timeout",
        "dagster.code_location_host": "code_location_host",
        "dagster.code_location_port": "code_location_port",
        "dagster.code_location_port_segment_size": "code_location_port_segment_size",
        "dagster.code_location_port_max_segments": "code_location_port_max_segments",
        "dagster.code_location_advertise_host": "code_location_advertise_host",
        "dagster.home": "dagster_home",
        "dagster.daemon_enabled": "dagster_daemon_enabled",
        "dagster.webserver_enabled": "dagster_webserver_enabled",
        "dagster.webserver_host": "dagster_webserver_host",
        "dagster.webserver_port": "dagster_webserver_port",
        "dagster.graphql_url": "dagster_graphql_url",
        "dagster.remote_graphql_url": "dagster_remote_graphql_url",
        "dagster.process_start_timeout": "process_start_timeout",
        "dagster.process_stop_timeout": "process_stop_timeout",
        "dagster.process_health_interval": "process_health_interval",
        "dagster.process_max_restarts": "process_max_restarts",
        "dagster.process_restart_window": "process_restart_window",
        "executor.enabled": "executor_enabled",
        "executor.mode": "executor_mode",
        "executor.container_image": "executor_container_image",
        "executor.container_ready_timeout": "executor_container_ready_timeout",
        "executor.user_base": "executor_user_base",
        "executor.socket_dir": "executor_socket_dir",
        "executor.idle_stop_seconds": "executor_idle_stop_seconds",
        "executor.max_restarts": "executor_max_restarts",
        "executor.restart_window_seconds": "executor_restart_window_seconds",
        "executor.address_space_bytes": "executor_address_space_bytes",
        "executor.cpu_seconds": "executor_cpu_seconds",
        "executor.max_processes": "executor_max_processes",
        "executor.max_file_size_bytes": "executor_max_file_size_bytes",
        "executor.max_open_files": "executor_max_open_files",
        "executor.shell_timeout_seconds": "executor_shell_timeout_seconds",
        "materialize.root": "materialize_root",
        "materialize.mount_kind": "materialize_mount_kind",
        "materialize.disk_high_watermark_percent": "materialize_disk_high_watermark_percent",
        "materialize.disk_low_watermark_percent": "materialize_disk_low_watermark_percent",
        "materialize.reclaim_interval_seconds": "materialize_reclaim_interval_seconds",
        "lease.enabled": "lease_enabled",
        "lease.ttl_seconds": "lease_ttl_seconds",
        "lease.max_per_instance": "lease_max_per_instance",
        "lease.admission_rss_high_watermark": "lease_admission_rss_high_watermark",
        "lease.idle_release_seconds": "lease_idle_release_seconds",
        "lease.quota_retry_after_seconds": "lease_quota_retry_after_seconds",
        "materialize.wip_snapshot_interval_seconds": "wip_snapshot_interval_seconds",
        "materialize.wip_snapshot_skip_if_clean": "wip_snapshot_skip_if_clean",
        "warm_pool.enabled": "warm_pool_enabled",
        "warm_pool.max_pods": "warm_pool_max_pods",
        "warm_pool.max_tasks": "warm_pool_max_tasks",
        "warm_pool.idle_seconds": "warm_pool_idle_seconds",
        "warm_pool.ready_timeout": "warm_pool_ready_timeout",
        "warm_pool.kinds": "warm_pool_kinds",
    }
    return flatten_yaml_mapping(data, mapping)


settings = Settings()
