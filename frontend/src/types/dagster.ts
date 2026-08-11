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

/** A managed Code Location (gRPC server). */
export interface CodeLocationInfo {
  location_name: string
  host: string
  port: number
  definitions: string | null
}

/** Request body for launching a run. */
export interface TriggerRunRequest {
  workspace_path: string
  job_name: string
  repository_name?: string
  run_config?: Record<string, unknown>
}
