/**
 * TypeScript types for the ontology (semantic layer) domain.
 *
 * These types mirror the ontology-service Pydantic models (see
 * `ontology-service/src/models.py`). The full semantic model is fetched from
 * `GET /api/v1/ontology/{name}` and rendered by the ontology detail view
 * (overview / graph / metrics tabs).
 *
 * Ontologies are user-authored YAML files stored under
 * `{workspace}/ontology/{name}.yaml`; the file stem is the ontology name.
 */

// ─────────── Lightweight summary used by the sidebar list ───────────
export interface OntologySummary {
  name: string // File stem (e.g. "sushi_retail")
  path: string // Relative path inside the workspace (e.g. "ontology/sushi_retail.yaml")
  description: string // Top-level `description` field; empty if not set
}

// ─────────── AI context (attachable at every level) ───────────
export interface AIContext {
  instructions?: string | null
  synonyms: string[]
  examples: string[]
}

// AI context may be a plain string shorthand or the structured object.
export type AIContextValue = string | AIContext

// ─────────── Entity sub-structures ───────────
export type AttributeType = 'categorical' | 'time' | 'numeric' | 'text'

export interface Attribute {
  name: string
  column: string
  type: AttributeType
  granularity?: string | null
  description?: string | null
  ai_context?: AIContextValue | null
}

export interface ForeignKey {
  name: string
  column: string
  references: string // e.g. "Customer.customer_id"
}

export interface SubConcept {
  name: string
  derived_by: string | string[]
  description?: string | null
  ai_context?: AIContextValue | null
}

// ─────────── Entity ───────────
export type EntityType = 'EntityType' | 'ValueType'

export interface Entity {
  name: string
  description?: string | null
  type: EntityType
  model?: string | null // SQLMesh fully-qualified model name
  key: Record<string, string> // concept attribute -> physical column
  attributes: Attribute[]
  foreign_keys: ForeignKey[]
  sub_concepts: Record<string, SubConcept>
  extends: string[]
  derived_by: string[]
  identify_by: string[]
  requires: string[]
  ai_context?: AIContextValue | null
}

// ─────────── Relationship ───────────
// The backend serialises the YAML `from` / `to` keys via aliases, so the wire
// shape uses `from` / `to`. The store normalises these into `from_entity` /
// `to_entity` so the UI never has to touch the reserved-word `from`.
export type Multiplicity = 'ManyToOne' | 'OneToOne'

export interface Relationship {
  name: string
  from_entity: string
  to_entity: string
  join: string[][] // [[from_attr, to_attr], ...]
  via_attribute?: string | null
  multiplicity?: Multiplicity | null
  description?: string | null
  verbalizes: string[]
  derived_by: string[]
  requires: string[]
  ai_context?: AIContextValue | null
}

// ─────────── Metric ───────────
export type AggregateFn = 'COUNT' | 'SUM' | 'AVG' | 'MIN' | 'MAX' | 'COUNT_DISTINCT'

export interface DerivedMetric {
  formula: string
}

export interface Metric {
  name: string
  description?: string | null
  aggregate?: AggregateFn | null
  expression?: string | null // e.g. "Order.order_id"
  entity?: string | null
  entities: string[]
  filter?: string | null
  derived?: DerivedMetric | null
  maps_to?: string | null // references an existing SQLMesh METRIC
  ai_context?: AIContextValue | null
}

// ─────────── Top-level semantic model ───────────
export interface SemanticModel {
  version: string
  name: string
  description?: string | null
  ai_context?: AIContextValue | null
  entities: Record<string, Entity>
  relationships: Record<string, Relationship>
  metrics: Record<string, Metric>
}

// ─────────── Validation ───────────
export type ValidationSeverity = 'error' | 'warning'

export interface OntologyValidationError {
  path: string
  message: string
  severity: ValidationSeverity
}

// Response envelope of GET /api/v1/ontology/{name}.
export interface OntologyModelResult {
  model: SemanticModel
  validation_errors: OntologyValidationError[]
  sqlmesh_checked: boolean
}
