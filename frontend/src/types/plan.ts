/**
 * Plan & Apply type definitions.
 * Models the SQLMesh plan/apply lifecycle states, options, and API responses.
 */

/** Plan action state machine */
export type PlanActionState =
  | 'idle'
  | 'running'
  | 'preview'
  | 'applying'
  | 'cancelling'
  | 'done'
  | 'error'

/** Plan options matching backend PlanOptions model */
export interface PlanOptions {
  skip_tests: boolean
  skip_backfill: boolean
  forward_only: boolean
  auto_apply: boolean
  no_auto_categorization: boolean
  include_unmodified: boolean
  no_gaps: boolean
  create_from?: string
  restate_models?: string[]
}

/** Plan dates for backfill range (UTC) */
export interface PlanDates {
  start?: string
  end?: string
}

/** Single model change in plan result */
export interface PlanChange {
  name: string
  view_name?: string
  display_view_name?: string
  diff?: string
  change_category?: string
  node_type?: string
  indirect?: PlanChange[]
  direct?: PlanChange[]
}

/** Grouped changes from plan preview */
export interface PlanChanges {
  added: PlanChange[]
  removed: PlanChange[]
  direct: PlanChange[]
  indirect: PlanChange[]
  metadata: PlanChange[]
}

/** Backfill detail for a single model */
export interface BackfillDetail {
  name: string
  view_name?: string
  interval: string[]
  batches: number
}

/** Single failed/errored test case detail */
export interface TestFailureDetail {
  message: string
  details: string
}

/** Structured unit test failure results (matches backend PlanTestResults) */
export interface TestResults {
  total: number
  failures: number
  errors: number
  successful: number
  dialect: string
  details: TestFailureDetail[]
  traceback: string
}

/** Single linter violation attached to a model file (matches backend PlanLintViolation). */
export interface LintViolation {
  model_name: string
  file_path: string
  rule: string
  message: string
  severity: 'error' | 'warning'
  line?: number | null
  column?: number | null
}

/** Structured linter failure results (matches backend PlanLintResults). */
export interface LintResults {
  total: number
  errors: number
  warnings: number
  by_file: LintViolation[]
  // Native-CLI-style grouped text (optional; used for a verbatim display).
  report?: string
  message: string
}

/** Plan preview API response */
export interface PlanResult {
  environment?: string
  has_changes: boolean
  requires_backfill: boolean
  changes?: {
    added?: PlanChange[]
    removed?: PlanChange[]
    modified?: {
      direct?: PlanChange[]
      indirect?: PlanChange[]
      metadata?: PlanChange[]
    }
  }
  backfills?: BackfillDetail[]
  state_fingerprint?: string
  start?: string
  end?: string
  // Test execution status
  tests_skipped?: boolean
  tests_passed?: boolean
  tests_failed?: boolean
  test_results?: TestResults
  // Lint execution status
  lint_failed?: boolean
  lint_results?: LintResults
}

/**
 * Plan API error response. The backend can return either a structured
 * ServiceError (with `error_code` / `message` / `lint_results`) on 4xx, or
 * a generic 500 payload. We keep both shapes in one type so the store can
 * surface the most specific message available.
 */
export interface PlanApiError {
  error_code?: string
  message?: string
  detail?: string | Record<string, unknown>
  lint_results?: LintResults
  // Catch-all for unrecognized 500-class bodies (e.g. FastAPI default).
  [key: string]: unknown
}

/** Apply progress stage status */
export type StageStatus = 'pending' | 'running' | 'success' | 'fail'

/** Apply progress stage */
export interface ApplyStage {
  name: string
  status: StageStatus
  num_tasks?: number
  total_tasks?: number
}

/** Default plan options */
export const DEFAULT_PLAN_OPTIONS: PlanOptions = {
  skip_tests: false,
  skip_backfill: false,
  forward_only: false,
  auto_apply: false,
  no_auto_categorization: false,
  include_unmodified: true,
  no_gaps: false,
}

// --- Apply History ---

/** Tab type for PlanPanel */
export type PlanTab = 'plan' | 'history'

/** Summary of a single apply history record (list endpoint) */
export interface ApplyHistoryRecord {
  id: string
  workspace_path: string
  environment: string
  user_id: string
  session_id: string
  trigger_type: 'human' | 'agent'
  applied: boolean
  reason?: string
  has_changes: boolean
  added_count: number
  removed_count: number
  direct_count: number
  indirect_count: number
  metadata_count: number
  backfill_count: number
  duration_ms: number
  error_message?: string
  created_at: string  // UTC ISO 8601
}

/** Full apply history detail (detail endpoint, includes plan_result) */
export interface ApplyHistoryDetail extends ApplyHistoryRecord {
  plan_result?: PlanResult
}

/** Paginated history list response */
export interface ApplyHistoryListResponse {
  records: ApplyHistoryRecord[]
  total: number
}
