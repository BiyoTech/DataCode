/**
 * TypeScript types for SQLMesh models domain.
 * Matches the backend responses from /api/v1/sqlmesh/models, /models/detail, /lineage, /apply/history.
 */

// ─────────── Model list ───────────
export interface ModelSummary {
  name: string
  fqn: string
  path: string
  dialect: string
  type: 'sql' | 'python' | 'seed' | 'external' | 'source'
  kind: string
  description: string
  owner: string
  tags: string[]
  cron: string
  /** Gateway the model is bound to (populated for external models). */
  gateway?: string
}

// ─────────── Model detail (with columns / schema) ───────────
export interface ModelColumn {
  name: string
  type: string
  description: string
}

export interface ModelReference {
  name: string
  expression: string
  unique: boolean
}

export interface ModelDetails {
  owner: string
  kind: string
  batch_size: number | null
  cron: string
  stamp: string | null
  start: string | null
  retention: string | null
  table_format: string | null
  storage_format: string | null
  time_column: string | null
  tags: string
  references: ModelReference[]
  partitioned_by: string | null
  clustered_by: string | null
  lookback: number | null
  cron_prev: string | null
  cron_next: string | null
  interval_unit: string | null
  annotated: boolean
}

export interface ModelDetail {
  name: string
  fqn: string
  path: string
  full_path: string
  dialect: string
  type: string
  columns: ModelColumn[]
  description: string
  details: ModelDetails
  sql: string
  default_catalog: string
  hash: string
  /** Gateway the model is bound to (populated for external models). */
  gateway?: string
}

// ─────────── Model lineage ───────────
export type ModelGraph = Record<string, string[]>

export interface LineageNodeColumn {
  name: string
  type: string
}

export interface LineageNode {
  name: string
  fqn: string
  type: string
  kind: string
  columns: LineageNodeColumn[]
}

export type LineageNodeMap = Record<string, LineageNode>

export interface ModelLineageResponse {
  graph: ModelGraph
  nodes: LineageNodeMap
}

export interface ColumnLineageNode {
  // expression/source are only present when models_only=false (CTE detail mode)
  expression?: string
  source?: string
  // Upstream model-level dependencies: { upstream_model_fqn: [column, ...] }
  models: Record<string, string[]>
}

// Inner adjacency: { model_fqn: { column_name: ColumnLineageNode } }
export type ColumnLineageAdjacency = Record<string, Record<string, ColumnLineageNode>>

// API wrapper returned by GET /lineage/{model}/{column}
export interface ColumnLineageResult {
  lineage: ColumnLineageAdjacency
}

// ─────────── Model diff (from plan context_diff) ───────────
export interface ModelDiffRequest {
  model_name: string
}

// ─────────── Tree structure for sidebar ───────────
export interface ModelTreeNode {
  name: string
  fullName: string
  isDirectory: boolean
  children?: ModelTreeNode[]
  model?: ModelSummary
}
