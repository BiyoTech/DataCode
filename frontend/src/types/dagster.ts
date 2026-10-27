/**
 * TypeScript types for the Dagster (Asset Dag) domain.
 *
 * These mirror the dagster-service REST response models. All timestamps are
 * epoch seconds in UTC; the frontend converts them to the user's local
 * timezone for display (see utils/timezone.ts).
 */

/** Active Dagster deployment mode. */
export type DagsterMode = 'local' | 'remote'

/** Mode + endpoint info returned by GET /dagster/mode. */
export interface ModeInfo {
  mode: DagsterMode
  graphql_url: string
  code_location_host: string
  code_location_port: number
}

/** Per-subprocess status returned by GET /dagster/health. */
export interface ProcessStatus {
  name: string
  pid: number | null
  alive: boolean
  uptime_seconds: number | null
}

/** Aggregated health payload. */
export interface HealthResponse {
  status: string
  service: string
  dagster_mode: DagsterMode
  processes: ProcessStatus[]
}

/** A single asset node in the DAG. */
export interface AssetInfo {
  asset_key: string[]
  asset_key_str: string
  // Human-friendly display name. For SQLMesh-bridged assets this is the model
  // display name (`schema.table`) matching /api/v1/sqlmesh/models; for native
  // Dagster assets it falls back to the asset key string.
  name?: string | null
  group_name: string | null
  description: string | null
  kind: string | null
  deps: string[]
}

/** Directed edge between two asset keys (source -> target). */
export interface AssetDagEdge {
  source: string
  target: string
}

/** Full asset dependency graph for rendering. */
export interface AssetDagGraph {
  nodes: AssetInfo[]
  edges: AssetDagEdge[]
}

/**
 * A single node in a SQLMesh lineage response.
 *
 * Returned by GET /sqlmesh/lineage; the map is keyed by model FQN. ``name`` is
 * the display name that matches the SQLMesh model list and the Dagster asset
 * key derived from it.
 */
export interface SqlmeshLineageNode {
  name: string
  fqn: string
  type: string
  kind: string
  columns: Array<{ name: string; type: string }>
}

/**
 * SQLMesh lineage response shape (GET /sqlmesh/lineage).
 *
 * ``graph`` is an adjacency map {fqn: [upstream_fqn, ...]}; ``nodes`` carries
 * per-node metadata keyed by the same FQN.
 */
export interface SqlmeshLineageResponse {
  graph: Record<string, string[]>
  nodes?: Record<string, SqlmeshLineageNode>
}

/** Run lifecycle status (Dagster canonical statuses, lower-cased). */
export type RunStatus =
  | 'queued'
  | 'not_started'
  | 'starting'
  | 'started'
  | 'success'
  | 'failure'
  | 'canceling'
  | 'canceled'

/** A pipeline/job run. */
export interface RunInfo {
  run_id: string
  status: string
  start_time: number | null
  end_time: number | null
  job_name: string | null
}

/** A single structured log event for a run. */
export interface RunLogEvent {
  message: string | null
  timestamp: string | null
  level: string | null
  step_key: string | null
}

/** A schedule definition. */
export interface ScheduleInfo {
  name: string
  cron_schedule: string | null
  pipeline_name: string | null
  status: string | null
}

/**
 * A Dagster deployment: one environment's code location + run namespace.
 *
 * ``location_name`` equals ``deployment_name``; both are exposed so callers can
 * speak either vocabulary. ``schedules_enabled`` tells whether the daemon may
 * run this environment's schedules — false for every branch deployment unless
 * the platform granted it explicitly.
 */
export interface DeploymentInfo {
  deployment_name: string
  location_name: string
  workspace_path: string
  tenant_id: string
  workspace_id: string
  environment: string
  env_type: 'production' | 'shared' | 'sandbox'
  schedules_enabled: boolean
  host: string
  port: number
  definitions: string | null
  alive: boolean
  /** Dependency state, attached by `ensure`. Never means an install happened. */
  deps?: DepsState | null
}

/** State of a working copy's declared Python dependencies. */
export type DepsStateName =
  | 'ready'
  | 'drifted'
  | 'missing'
  | 'incompatible'
  | 'failed'
  | 'empty'
  | 'declaration_invalid'

/**
 * One declared requirement the platform baseline cannot satisfy.
 *
 * Structured rather than a sentence, because the UI offers the rewrite as an
 * action: `suggested` is the exact string to put in the file. A lower bound and
 * not a pin — the baseline moves with the platform image.
 */
export interface DepsConflict {
  /** The requirement as declared, e.g. `pyarrow<15.0.0,>=14.0.0`. */
  requirement: string
  name: string
  platform_version: string
  /** What to declare instead, e.g. `pyarrow>=25.0.1`. */
  suggested: string
}

/**
 * A declared package whose OWN dependencies clash with the platform baseline.
 *
 * The difference from `DepsConflict` is which package the blocker is: here the
 * declaration never mentions it (`feast` needs `tenacity<9`), so no check of
 * the file can find it and it only surfaces after a resolve. `suggested` is
 * the rewritten requirement pip was actually able to resolve; empty means no
 * release of that package fits this baseline.
 *
 * Seeing one of these means the platform already tried releasing every pin it
 * was allowed to release: a pure-Python platform package is overridden for the
 * copy automatically (see `DepsRelaxedPin`), so what is left is either an edit
 * or `protected` — a pin only an operator can move.
 */
export interface DepsTransitiveConflict {
  /** The requirement as declared, e.g. `feast[postgres]<0.66.0,>=0.60.0`. */
  requirement: string
  /** Canonical name of the DECLARED package — the line that has to change. */
  name: string
  /**
   * Platform packages this requirement genuinely cannot accept.
   *
   * `required` is the version RANGE a dependency states (`>=7,<9`), not a
   * version, and `required_by` names the dependency stating it — neither
   * appears anywhere in the user's file.
   */
  blockers: Array<{
    name: string
    required: string
    platform_version: string
    required_by?: string
  }>
  /** What to declare instead; empty when nothing fits. */
  suggested: string
  /** What `suggested` resolves to, so the range is not taken on faith. */
  resolved_version: string
  /**
   * Blockers the platform will never yield — a compiled extension, or part of
   * its own runtime. Non-empty with an empty `suggested` is the one case where
   * the honest answer is "ask an operator".
   */
  protected?: string[]
}

/**
 * A platform package this working copy's overlay overrides.
 *
 * The platform releases a pin only when keeping it made the declaration
 * unsatisfiable, and only for packages whose replacement is local to the copy
 * (no compiled extension, not part of the platform's own runtime). Surfaced
 * because the override outlives the install that made it: from here on this
 * copy imports a different version than the platform does.
 */
export interface DepsRelaxedPin {
  name: string
  /** What the platform itself ships. */
  platform_version: string
  /** What this working copy now uses. */
  overlay_version: string
  /** The dependency that required the other version, when known. */
  required_by?: string
}

/**
 * Read-only dependency status of a working copy.
 *
 * Produced by `ensure` from three file reads: the declaration in
 * `asset-dag/pyproject.toml`, the installed manifest and the overlay directory.
 * Anything other than `ready` / `empty` means a reload has work to do — the
 * install itself only ever runs inside a reload job the user triggered.
 */
export interface DepsState {
  state: DepsStateName
  declared: number
  installed: number
  fingerprint: string
  /** Why the state is what it is, when it needs saying. Rendered verbatim. */
  message: string
  /**
   * Requirements that conflict with the platform baseline.
   *
   * Non-empty means a reload CANNOT succeed, whatever `state` says — which is
   * why it is reported by the read path and not only by a failed install.
   */
  conflicts?: DepsConflict[]
  /**
   * Platform packages this copy's overlay overrides.
   *
   * Reported by the read path, not only by the install that yielded them: the
   * divergence is a standing property of the copy, and whoever opens the page
   * next has to be able to see it.
   */
  relaxed?: DepsRelaxedPin[]
  /** UTC ISO-8601; convert to the user's timezone before display. */
  installed_at: string
  size_bytes: number
}

/** One distribution the platform provides without any declaration. */
export interface BaselinePackage {
  name: string
  version: string
  /** The requirement string to declare for it, if one wants to be explicit. */
  suggested: string
}

/**
 * What a code location can import with no overlay at all.
 *
 * A property of the runtime image, identical for every working copy, which is
 * why the endpoint takes no workspace: it is the reference a user (or the
 * agent) needs in order to write a declaration that can install at all.
 */
export interface DepsBaseline {
  packages: BaselinePackage[]
  /** The subset user code commonly imports — several hundred is unreadable. */
  highlighted: BaselinePackage[]
  python_version: string
  platform_tag: string
}

/** Verdict returned by `POST /files/write` when the saved file is the declaration. */
export interface DepsDeclarationDiagnostics {
  valid: boolean
  message: string
  conflicts: DepsConflict[]
  declared?: number
}

/** Stages a reload passes through, in order. */
export type ReloadStage =
  | 'queued'
  | 'resolving_deps'
  | 'installing_deps'
  | 'starting_code_location'
  | 'loading_definitions'
  | 'ready'
  | 'failed'

export type ReloadJobState = 'running' | 'succeeded' | 'failed' | 'cancelled'

/**
 * One reload attempt.
 *
 * A reload installs the working copy's declared packages before it starts the
 * code location, so it can take minutes. The POST returns this envelope
 * immediately; progress arrives on the SSE `deployment` topic and the same shape
 * is readable from the status endpoint, which is the authoritative truth after a
 * reconnect, a page refresh or a second tab.
 */
export interface ReloadJobInfo {
  job_id: string
  workspace_path: string
  deployment_name: string
  state: ReloadJobState
  stage: ReloadStage
  /** The stage ladder, so the UI needs no copy of it. */
  stages: ReloadStage[]
  message: string
  started_at: string
  finished_at: string
  /** This response joined a reload that was already running. */
  attached: boolean
  failure_code: string
  /**
   * Set when `failure_code` is `deps_platform_conflict`.
   *
   * The same structure the read-only status carries, so a failed reload offers
   * the identical "apply this rewrite" action instead of only a paragraph.
   */
  conflicts?: DepsConflict[]
  /**
   * Set when `failure_code` is `deps_platform_transitive`.
   *
   * Same idea as `conflicts` for the one shape a declaration check cannot see,
   * so it is only known once the installer has resolved — which is why it can
   * only ever come from a failed reload, never from the read-only status.
   */
  transitive_conflicts?: DepsTransitiveConflict[]
  /**
   * Set on a SUCCESSFUL reload that had to override platform packages to
   * install the declaration. Published as soon as the install finishes, so the
   * panel explains it while the code location is still starting.
   */
  relaxed?: DepsRelaxedPin[]
  /** Installer transcript or import traceback; selectable and copyable. */
  output_tail: string
  /** Record under {workspace}/logs/errors/ holding the full failure. */
  error_log_id: string
  deps_state: string
  deployment: DeploymentInfo | null
  /** One new transcript line, present only on progress events. */
  line?: string
}

/** Asset-key level difference between two deployments. */
export interface DeploymentAssetDiff {
  added: string[]
  changed: string[]
  removed: string[]
}

/** One schedule whose definition differs between two deployments. */
export interface DeploymentScheduleChange {
  name: string
  /** added | removed | modified */
  change: string
  cron: string
  base_cron: string
  job: string
}

/**
 * Orchestration-graph difference between a deployment and its base
 * (GET /dagster/deployments/diff). Both deployments must be loaded for the
 * service to read their definition sets.
 */
export interface DeploymentDiff {
  deployment: string
  base: string
  assets: DeploymentAssetDiff
  schedule_changes: DeploymentScheduleChange[]
}

/** Request body for launching a run. */
export interface TriggerRunRequest {
  workspace_path: string
  job_name: string
  repository_name?: string
  run_config?: Record<string, unknown>
}
