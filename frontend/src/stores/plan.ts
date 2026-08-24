/**
 * Plan Store - manages SQLMesh Plan/Apply lifecycle and Apply History.
 * Plan state machine: idle → running → preview → applying → done/error
 * Supports cancel at any processing stage.
 * History tab: browse past apply records with pagination.
 */
import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import api from '@/utils/api'
import { extractApiMessage, notifyError } from '@/utils/notifyError'
import { useWorkspaceStore } from '@/stores/workspace'
import { useEnvironmentStore } from '@/stores/environment'
import { useGatewayStore } from '@/stores/gateway'
import type {
  PlanActionState,
  PlanOptions,
  PlanDates,
  PlanResult,
  PlanChanges,
  BackfillDetail,
  TestResults,
  LintResults,
  PlanApiError,
  ApplyStage,
  PlanTab,
  ApplyHistoryRecord,
  ApplyHistoryDetail,
  ApplyHistoryListResponse,
} from '@/types/plan'
import { DEFAULT_PLAN_OPTIONS } from '@/types/plan'

const HISTORY_PAGE_SIZE = 50

/**
 * Wrapped POST: on HTTP 4xx/5xx, returns the parsed error body instead of
 * throwing. The default axios behavior raises an exception, which the caller
 * has to remember to convert; here we make success/error explicit so the
 * store can branch on the structured ``error_code``.
 */
async function safePost<T, E>(
  url: string,
  body: Record<string, unknown>,
): Promise<{ data: T | null; error: E | null }> {
  try {
    const { data } = await api.post<T>(url, body)
    return { data, error: null }
  } catch (err: any) {
    const payload = err?.response?.data
    // Authorization denials do not describe a plan problem: replace the body
    // with the localized permission message so the panel states what is missing.
    if (err?.response?.status === 403) {
      return { data: null, error: { message: extractApiMessage(err) } as E }
    }
    return {
      data: null,
      error: (payload && typeof payload === 'object' ? payload : { message: err?.message }) as E,
    }
  }
}

/**
 * Convert a backend error payload into a user-facing message + optional
 * structured lint results. Keeps the store call sites free of nested
 * ``err.response.data.detail.message`` ladders.
 */
function parsePlanError(payload: PlanApiError): { message: string; lintResults: LintResults | null } {
  // 1) Lint failure: top-level `lint_results` (nested shape, if ever used).
  if (payload.lint_results && typeof payload.lint_results === 'object') {
    const lr = payload.lint_results as LintResults
    return {
      message: payload.message || lr.report || lr.message || 'Plan blocked by linter errors.',
      lintResults: lr,
    }
  }

  // 2) Lint failure: structured fields INLINED at the top level by
  // ``to_response_dict`` (``by_file``/``report``/``total`` live directly on the
  // body). Reconstruct a ``LintResults`` so the panel can render diagnostics.
  const anyPayload = payload as any
  if (Array.isArray(anyPayload.by_file)) {
    const lr: LintResults = {
      total: anyPayload.total ?? anyPayload.by_file.length,
      errors: anyPayload.errors ?? 0,
      warnings: anyPayload.warnings ?? 0,
      by_file: anyPayload.by_file,
      report: anyPayload.report ?? '',
      message: anyPayload.detail_message ?? '',
    }
    return {
      message: payload.message || lr.report || lr.message || 'Plan blocked by linter errors.',
      lintResults: lr,
    }
  }

  // 3) Top-level message: the backend's PRIMARY, actionable root cause (see
  // ``ServiceError.to_response_dict``). Checked BEFORE ``detail`` so a
  // structured error (e.g. STATE_SCHEMA_MISMATCH) never gets its message
  // replaced by the more verbose supplementary detail.
  if (typeof payload.message === 'string' && payload.message) {
    return { message: payload.message, lintResults: null }
  }

  // 4) Legacy 4xx shape with `detail` as a string.
  if (typeof anyPayload.detail === 'string') {
    return { message: anyPayload.detail as string, lintResults: null }
  }

  // 5) Legacy 4xx shape with `detail` as a dict (some errors wrap ``message``).
  const detail = anyPayload.detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    const lr = (detail as any).lint_results as LintResults | undefined
    return {
      message: detail.message as string,
      lintResults: lr ?? null,
    }
  }

  // 6) Fallback for unrecognized 500 bodies.
  return { message: 'Plan failed (unexpected error response).', lintResults: null }
}

export const usePlanStore = defineStore('plan', () => {
  // --- State ---
  const actionState = ref<PlanActionState>('idle')
  const options = ref<PlanOptions>({ ...DEFAULT_PLAN_OPTIONS })
  const planDates = ref<PlanDates>({})
  const planResult = ref<PlanResult | null>(null)
  const applyStages = ref<ApplyStage[]>([])
  const errorMessage = ref('')
  // Structured context of the last error: ``errorCode`` lets the UI pick a
  // dedicated surface (e.g. the state-schema repair card) instead of a plain
  // message banner; ``errorPayload`` carries the backend's inlined fields
  // (gateway / expected / actual for STATE_SCHEMA_MISMATCH).
  const errorCode = ref('')
  const errorPayload = ref<PlanApiError | null>(null)
  const isLoading = ref(false)

  // --- History State ---
  const activeTab = ref<PlanTab>('plan')
  const historyRecords = ref<ApplyHistoryRecord[]>([])
  const historyLoading = ref(false)
  const historyTotal = ref(0)
  const historyOffset = ref(0)
  const selectedHistoryId = ref('')
  const selectedHistoryDetail = ref<ApplyHistoryDetail | null>(null)
  const historyDetailLoading = ref(false)

  // --- Computed ---
  const isIdle = computed(() => actionState.value === 'idle')
  const isRunning = computed(() => actionState.value === 'running')
  const isPreview = computed(() => actionState.value === 'preview')
  const isApplying = computed(() => actionState.value === 'applying')
  const isCancelling = computed(() => actionState.value === 'cancelling')
  const isDone = computed(() => actionState.value === 'done')
  const isError = computed(() => actionState.value === 'error')
  const isProcessing = computed(() =>
    ['running', 'applying', 'cancelling'].includes(actionState.value)
  )
  const hasChanges = computed(() => planResult.value?.has_changes ?? false)
  const requiresBackfill = computed(() => planResult.value?.requires_backfill ?? false)

  // Normalize backend's nested change structure into flat groups for the UI.
  const changes = computed<PlanChanges>(() => {
    const c = planResult.value?.changes
    return {
      added: c?.added ?? [],
      removed: c?.removed ?? [],
      direct: c?.modified?.direct ?? [],
      indirect: c?.modified?.indirect ?? [],
      metadata: c?.modified?.metadata ?? [],
    }
  })

  const backfills = computed<BackfillDetail[]>(() => planResult.value?.backfills ?? [])

  // Test execution status from the latest plan result.
  const testResults = computed<TestResults | null>(() => planResult.value?.test_results ?? null)
  const testsFailed = computed(() => planResult.value?.tests_failed ?? false)
  const testsSkipped = computed(() => planResult.value?.tests_skipped ?? false)
  const testsPassed = computed(() => planResult.value?.tests_passed ?? false)

  // Lint execution status from the latest plan result.
  const lintResults = computed<LintResults | null>(() => planResult.value?.lint_results ?? null)
  const lintFailed = computed(() => planResult.value?.lint_failed ?? false)
  // Lint failures can also arrive as a 422 ServiceError without a successful
  // plan_result body — surface those too so the panel can render diagnostics.
  const lintResultsFromError = ref<LintResults | null>(null)
  const effectiveLintResults = computed<LintResults | null>(
    () => lintResults.value ?? lintResultsFromError.value,
  )
  const effectiveLintFailed = computed(
    () => lintFailed.value || effectiveLintResults.value !== null,
  )

  // --- Actions ---

  /** Run plan preview: calls POST /sqlmesh/plan */
  async function runPlan(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const environmentStore = useEnvironmentStore()
    const gatewayStore = useGatewayStore()
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath) {
      errorMessage.value = 'No workspace selected'
      actionState.value = 'error'
      return
    }

    actionState.value = 'running'
    isLoading.value = true
    errorMessage.value = ''
    errorCode.value = ''
    errorPayload.value = null
    planResult.value = null
    lintResultsFromError.value = null
    applyStages.value = []

    const { data, error } = await safePost<PlanResult, PlanApiError>('/sqlmesh/plan', {
      workspace_path: wsPath,
      environment: environmentStore.currentEnvironment,
      gateway: gatewayStore.currentGateway || undefined,
      plan_dates: planDates.value.start || planDates.value.end ? planDates.value : undefined,
      plan_options: options.value,
    })

    // The 422 path: lint errors don't yield a PlanResult body. Surface them
    // through the dedicated lint state so the panel renders the diagnostics
    // instead of an opaque "Request failed with status code 500".
    if (error) {
      const parsed = parsePlanError(error)
      errorMessage.value = parsed.message
      errorCode.value = typeof error.error_code === 'string' ? error.error_code : ''
      errorPayload.value = error
      lintResultsFromError.value = parsed.lintResults
      actionState.value = 'error'
      isLoading.value = false
      return
    }

    if (!data) return

    planResult.value = data

    // Tests failed: keep the structured results and move to error state so the
    // UI renders the "Plan Failed / Tests Failed" panel instead of changes.
    if (data.tests_failed) {
      actionState.value = 'error'
      errorMessage.value = 'One or more tests failed'
      isLoading.value = false
      return
    }

    actionState.value = 'preview'
    isLoading.value = false

    // Auto-apply if option is set
    if (options.value.auto_apply && data.has_changes) {
      await applyPlan()
    }
  }

  /** Apply plan: calls POST /sqlmesh/apply */
  async function applyPlan(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const environmentStore = useEnvironmentStore()
    const gatewayStore = useGatewayStore()
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath || !planResult.value) return

    actionState.value = 'applying'
    isLoading.value = true
    errorMessage.value = ''
    errorCode.value = ''
    errorPayload.value = null
    lintResultsFromError.value = null
    applyStages.value = [
      { name: 'creation', status: 'pending' },
      { name: 'backfill', status: 'pending' },
      { name: 'promote', status: 'pending' },
    ]

    const { data, error } = await safePost<any, PlanApiError>('/sqlmesh/apply', {
      workspace_path: wsPath,
      environment: environmentStore.currentEnvironment,
      gateway: gatewayStore.currentGateway || undefined,
      plan_dates: planDates.value.start || planDates.value.end ? planDates.value : undefined,
      plan_options: options.value,
      expected_fingerprint: planResult.value.state_fingerprint,
    })

    if (error) {
      const parsed = parsePlanError(error)
      errorMessage.value = parsed.message
      errorCode.value = typeof error.error_code === 'string' ? error.error_code : ''
      errorPayload.value = error
      lintResultsFromError.value = parsed.lintResults
      applyStages.value = applyStages.value.map((s) => ({ ...s, status: 'fail' as const }))
      actionState.value = 'error'
      isLoading.value = false
      return
    }

    if (!data) return

    // Tests failed during apply: surface structured results.
    if (data.tests_failed) {
      planResult.value = { ...planResult.value, ...data }
      actionState.value = 'error'
      errorMessage.value = 'One or more tests failed'
      isLoading.value = false
      return
    }

    // Mark all stages as success on synchronous completion
    applyStages.value = applyStages.value.map(s => ({ ...s, status: 'success' as const }))
    actionState.value = 'done'
    isLoading.value = false
  }

  /** Cancel running plan/apply: calls POST /sqlmesh/cancel */
  async function cancelPlan(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const gatewayStore = useGatewayStore()
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath) return

    actionState.value = 'cancelling'

    await api.post('/sqlmesh/cancel', {
      workspace_path: wsPath,
      gateway: gatewayStore.currentGateway || undefined,
    }).catch((err) => {
      notifyError(err, 'Failed to cancel plan')
    })

    actionState.value = 'idle'
    isLoading.value = false
  }

  // --- History Computed ---

  const hasMoreHistory = computed(() => historyRecords.value.length < historyTotal.value)

  // Auto-switch tab: when plan operation starts, switch to Plan tab;
  // when it goes back to idle, stay on current tab.
  watch(actionState, (newState) => {
    if (newState !== 'idle') {
      activeTab.value = 'plan'
    }
  })

  // Auto-refresh history when workspace changes
  const workspaceStore = useWorkspaceStore()
  watch(() => workspaceStore.currentWorkspace?.path, (newPath) => {
    if (newPath) {
      resetHistory()
      // Load history eagerly when entering plan page with fresh workspace
    }
  })

  // --- History Actions ---

  /** Fetch apply history list (paginated) */
  async function fetchHistory(): Promise<void> {
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath) return

    historyLoading.value = true
    try {
      const { data } = await api.get<ApplyHistoryListResponse>('/sqlmesh/apply/history', {
        params: { workspace_path: wsPath, limit: HISTORY_PAGE_SIZE, offset: 0 },
      })
      historyRecords.value = data.records
      historyTotal.value = data.total
      historyOffset.value = data.records.length
    } catch (err: any) {
      notifyError(err, 'Failed to fetch apply history')
    } finally {
      historyLoading.value = false
    }
  }

  /** Load more history records (pagination) */
  async function fetchMoreHistory(): Promise<void> {
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath || !hasMoreHistory.value) return

    historyLoading.value = true
    try {
      const { data } = await api.get<ApplyHistoryListResponse>('/sqlmesh/apply/history', {
        params: { workspace_path: wsPath, limit: HISTORY_PAGE_SIZE, offset: historyOffset.value },
      })
      historyRecords.value.push(...data.records)
      historyOffset.value += data.records.length
    } catch (err: any) {
      notifyError(err, 'Failed to load more history')
    } finally {
      historyLoading.value = false
    }
  }

  /** Fetch detailed history record (with plan_result) */
  async function fetchHistoryDetail(recordId: string): Promise<void> {
    selectedHistoryId.value = recordId
    historyDetailLoading.value = true
    try {
      const { data } = await api.get<ApplyHistoryDetail>(`/sqlmesh/apply/history/${recordId}`)
      selectedHistoryDetail.value = data
    } catch (err: any) {
      notifyError(err, 'Failed to fetch history detail')
      selectedHistoryDetail.value = null
    } finally {
      historyDetailLoading.value = false
    }
  }

  /** Close the history detail view */
  function closeHistoryDetail(): void {
    selectedHistoryId.value = ''
    selectedHistoryDetail.value = null
  }

  /** Reset history state */
  function resetHistory(): void {
    historyRecords.value = []
    historyTotal.value = 0
    historyOffset.value = 0
    selectedHistoryId.value = ''
    selectedHistoryDetail.value = null
  }

  /** Switch to a specific tab */
  function switchTab(tab: PlanTab): void {
    activeTab.value = tab
    if (tab === 'history' && historyRecords.value.length === 0) {
      fetchHistory()
    }
  }

  /** Reset to initial state */
  function reset(): void {
    actionState.value = 'idle'
    planResult.value = null
    applyStages.value = []
    errorMessage.value = ''
    errorCode.value = ''
    errorPayload.value = null
    isLoading.value = false
  }

  /** Update a single plan option */
  function setOption<K extends keyof PlanOptions>(key: K, value: PlanOptions[K]): void {
    options.value[key] = value
  }

  /** Set plan date range (UTC strings) */
  function setDate(key: keyof PlanDates, value: string): void {
    planDates.value[key] = value || undefined
  }

  return {
    // State
    actionState,
    options,
    planDates,
    planResult,
    applyStages,
    errorMessage,
    errorCode,
    errorPayload,
    isLoading,
    // History State
    activeTab,
    historyRecords,
    historyLoading,
    historyTotal,
    selectedHistoryId,
    selectedHistoryDetail,
    historyDetailLoading,
    // Computed
    isIdle,
    isRunning,
    isPreview,
    isApplying,
    isCancelling,
    isDone,
    isError,
    isProcessing,
    hasChanges,
    requiresBackfill,
    changes,
    backfills,
    testResults,
    testsFailed,
    testsSkipped,
    testsPassed,
    lintResults: effectiveLintResults,
    lintFailed: effectiveLintFailed,
    hasMoreHistory,
    // Actions
    runPlan,
    applyPlan,
    cancelPlan,
    reset,
    setOption,
    setDate,
    // History Actions
    fetchHistory,
    fetchMoreHistory,
    fetchHistoryDetail,
    closeHistoryDetail,
    resetHistory,
    switchTab,
  }
})
