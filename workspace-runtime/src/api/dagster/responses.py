"""Response body models for the dagster-service REST API.

All timestamps are epoch seconds (UTC). The frontend converts to the user's
local timezone for display.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ModeInfo(BaseModel):
    """Active Dagster mode + endpoint info."""

    mode: str
    graphql_url: str
    code_location_host: str
    code_location_port: int


class ProcessStatus(BaseModel):
    name: str
    pid: Optional[int] = None
    alive: bool
    uptime_seconds: Optional[float] = None
    # Supervision state: how often the supervisor respawned this process inside
    # the current window, and whether it gave up on it.
    restart_count: int = 0
    abandoned: bool = False


class HealthResponse(BaseModel):
    status: str
    service: str
    dagster_mode: str
    processes: list[ProcessStatus] = []
    # Remote mode only: alive code locations the control plane has not confirmed
    # at their current revision. A non-empty list that does not drain means the
    # registration channel is broken and assets are going stale.
    pending_registrations: list[str] = []


class CodeLocationAdvertisementInfo(BaseModel):
    """One code location offered to the control plane for registration.

    ``host``/``port`` are the ADVERTISED (dialable) endpoint, not the bind
    address. ``revision`` increments on every (re)spawn; the control plane echoes
    it back on the heartbeat so a reload it has not yet picked up is
    distinguishable from one it already serves.
    """

    location_name: str
    host: str
    port: int
    revision: int
    acknowledged: bool = False


class CodeLocationRegistry(BaseModel):
    """What one dagster-service replica currently serves.

    The control plane merges these snapshots across replicas, keyed by
    ``replica_id``, to render its workspace file.
    """

    replica_id: str
    dagster_mode: str
    generated_at: float          # epoch seconds, UTC
    locations: list[CodeLocationAdvertisementInfo] = []
    # Alive locations awaiting control-plane confirmation.
    pending: list[str] = []
    # Heartbeat only: acknowledged names this replica does not run, so the
    # control plane can drop stale entries instead of dialing dead endpoints.
    unknown: list[str] = []


class AssetInfo(BaseModel):
    """A single asset node for DAG rendering."""

    asset_key: list[str]
    asset_key_str: str
    # Human-friendly display name. For SQLMesh-bridged assets this is the model
    # display name (``schema.table``) matching /api/v1/sqlmesh/models; for native
    # Dagster assets it falls back to the asset key string.
    name: Optional[str] = None
    group_name: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    deps: list[str] = []


class AssetDagGraph(BaseModel):
    """Full asset dependency graph (nodes + edges)."""

    nodes: list[AssetInfo] = []
    edges: list[dict[str, str]] = []  # {"source": key_str, "target": key_str}


class RunInfo(BaseModel):
    run_id: str
    status: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    job_name: Optional[str] = None


class RunLogEvent(BaseModel):
    message: Optional[str] = None
    timestamp: Optional[str] = None
    level: Optional[str] = None
    step_key: Optional[str] = None


class ScheduleInfo(BaseModel):
    name: str
    cron_schedule: Optional[str] = None
    pipeline_name: Optional[str] = None
    status: Optional[str] = None


class DepsConflict(BaseModel):
    """One declared requirement the platform baseline cannot satisfy.

    Structured rather than prose because the client offers the fix as an
    action: ``suggested`` is the exact string to write in the declaration, so
    neither the UI nor the agent re-derives the rule that the bound must be a
    lower bound (a pin would conflict again at the next image upgrade).
    """

    requirement: str = Field(description="The requirement as declared")
    name: str
    platform_version: str = Field(description="The version the platform ships")
    suggested: str = Field(description="What to declare instead")


class DepsBlocker(BaseModel):
    """A platform package a declaration genuinely cannot accept.

    Named because the declaration never mentions it: the user declared
    ``feast``, and this is the ``tenacity`` that one of feast's dependencies
    asked for. Without it the suggestion to change the ceiling looks arbitrary.

    ``required`` is the RANGE that dependency states (``>=7,<9``), not a
    version: a version would be whatever the index happened to have newest,
    which once turned a single real blocker into a list of ten.
    """

    name: str
    required: str = Field(
        description="The version RANGE a dependency states, e.g. >=7,<9"
    )
    platform_version: str = Field(description="The version the platform ships")
    required_by: str = Field(
        default="",
        description="The dependency that states it; the declaration does not",
    )


class DepsRelaxedPin(BaseModel):
    """A platform package this working copy's overlay overrides.

    The platform yields a pin only when keeping it would make the declaration
    unsatisfiable, and only for packages whose replacement affects this copy
    alone (no compiled extension, not part of the platform's own runtime).
    Reported because the override outlives the install: from here on this copy's
    processes import a different version than the platform's do.
    """

    name: str
    platform_version: str = Field(description="What the platform itself ships")
    overlay_version: str = Field(description="What this working copy now uses")
    required_by: str = Field(
        default="", description="The dependency that required the other version"
    )


class DepsTransitiveConflict(BaseModel):
    """A declared package whose OWN dependencies the baseline rejects.

    The mirror of :class:`DepsConflict` for the one shape a check of the
    declaration cannot see, so it is only ever known after a resolve — which is
    why it comes from a failed install and never from the read-only state.

    Reaching the client at all means the platform already tried releasing every
    pin it was allowed to release: what is left is either an edit
    (``suggested``) or a pin only an operator can move (``protected``).
    """

    requirement: str = Field(description="The requirement as declared")
    name: str = Field(description="Canonical name of the DECLARED package")
    blockers: list[DepsBlocker] = Field(default_factory=list)
    suggested: str = Field(
        default="",
        description=(
            "What to declare instead; empty when no release fits this baseline"
        ),
    )
    resolved_version: str = Field(
        default="", description="What `suggested` resolves to"
    )
    protected: list[str] = Field(
        default_factory=list,
        description=(
            "Blockers the platform will never yield — a compiled extension or "
            "part of its own runtime. Non-empty means the remaining fix is an "
            "operator's: upgrade the baseline, or allow the override"
        ),
    )


class DepsState(BaseModel):
    """Read-only state of a working copy's declared Python dependencies.

    Answered by ``ensure`` from three file reads. It NEVER means an install
    happened: the drift is reported so the UI can offer a reload, and the install
    only ever runs inside a reload job the user triggered.
    """

    state: str = Field(
        default="",
        description=(
            "ready | drifted | missing | incompatible | failed | empty | "
            "declaration_invalid"
        ),
    )
    declared: int = 0
    installed: int = 0
    fingerprint: str = ""
    # Present when the state needs explaining: a parse error, an interpreter
    # mismatch, the previous install's failure. Rendered verbatim.
    message: str = ""
    # Non-empty means a reload CANNOT succeed whatever `state` says, which is
    # why it is answered by the read path and not only by a failed install.
    conflicts: list[DepsConflict] = Field(default_factory=list)
    # Platform packages this copy overrides. Answered by the read path because
    # the override outlives the install that made it.
    relaxed: list[DepsRelaxedPin] = Field(default_factory=list)
    installed_at: str = Field(
        default="", description="UTC ISO-8601; the frontend localises it"
    )
    size_bytes: int = 0


class DeploymentInfo(BaseModel):
    """One environment's Dagster deployment and the state of its code location.

    ``location_name`` equals ``deployment_name``: the deployment IS the code
    location, exposed under both names so callers can speak either vocabulary.
    """

    deployment_name: str
    location_name: str
    workspace_path: str
    tenant_id: str
    workspace_id: str
    environment: str
    env_type: str
    schedules_enabled: bool
    host: str
    port: int
    definitions: Optional[str] = None
    alive: bool = False
    deps: Optional[DepsState] = None


class ReloadJobInfo(BaseModel):
    """One reload attempt, as observed at some point in time.

    Returned by ``POST /reload`` (accepted), by the status endpoint (authoritative
    truth) and pushed on the SSE ``deployment`` topic (progress). One shape for
    all three so a client that missed events can substitute a poll and see
    exactly the same fields.
    """

    job_id: str
    workspace_path: str
    deployment_name: str
    state: str = Field(description="running | succeeded | failed | cancelled")
    stage: str = Field(
        description=(
            "queued | resolving_deps | installing_deps | starting_code_location | "
            "loading_definitions | ready | failed"
        )
    )
    stages: list[str] = Field(
        default_factory=list,
        description="The stage ladder, in order, so the UI needs no copy of it",
    )
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    attached: bool = Field(
        default=False,
        description="This response joined a reload that was already running",
    )
    failure_code: str = Field(
        default="",
        description=(
            "deps_declaration_invalid | deps_platform_conflict | "
            "deps_platform_transitive | deps_install_failed | "
            "deps_no_wheel | deps_index_not_configured | "
            "deps_shadow_platform | deps_too_large | code_load_failed | "
            "sandbox_unavailable | internal_error"
        ),
    )
    conflicts: list[DepsConflict] = Field(
        default_factory=list,
        description=(
            "Set when failure_code is deps_platform_conflict: the requirements "
            "involved, each with the rewrite that would fix it"
        ),
    )
    transitive_conflicts: list[DepsTransitiveConflict] = Field(
        default_factory=list,
        description=(
            "Set when failure_code is deps_platform_transitive: declared "
            "packages whose dependencies clash with the platform baseline, "
            "each with the rewrite the installer verified"
        ),
    )
    relaxed: list[DepsRelaxedPin] = Field(
        default_factory=list,
        description=(
            "Set on a SUCCESSFUL reload that had to override platform packages "
            "for this working copy in order to install the declaration"
        ),
    )
    output_tail: str = Field(
        default="",
        description="Installer transcript or import traceback, for copying",
    )
    error_log_id: str = Field(
        default="",
        description=(
            "Record under {workspace}/logs/errors/ holding the full structured "
            "failure, readable by the agent with plain file tools"
        ),
    )
    deps_state: str = ""
    deployment: Optional[DeploymentInfo] = None


class DeploymentAssetDiff(BaseModel):
    """Asset-key level difference between two deployments."""

    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []


class DeploymentScheduleChange(BaseModel):
    """One schedule that differs between two deployments."""

    name: str
    change: str  # added | removed | modified
    cron: str = ""
    base_cron: str = ""
    job: str = ""


class DeploymentDiff(BaseModel):
    """Orchestration-graph difference between a deployment and its base."""

    deployment: str
    base: str
    assets: DeploymentAssetDiff
    schedule_changes: list[DeploymentScheduleChange] = []


class OperationResult(BaseModel):
    """Generic success envelope for mutations."""

    success: bool = True
    detail: Optional[dict[str, Any]] = None
