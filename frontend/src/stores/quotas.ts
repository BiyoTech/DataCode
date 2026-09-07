/**
 * Quota Store - the caller's own tenant budget and usage.
 *
 * Backs `GET /api/v1/quotas` (control-service via the gateway). Two jobs:
 *
 * 1. Render a quota panel (Settings) from `overview`.
 * 2. Let other flows explain a 429 immediately — when a creation is rejected
 *    with QUOTA_EXCEEDED, `refresh()` re-reads the server's view so the UI and
 *    the toast never disagree about the numbers.
 *
 * `current: null` is "unknown", not zero: the backend fails open exactly when it
 * cannot count, and rendering 0 at that moment would claim a full budget the
 * platform never measured. Consumers must keep that distinction.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '@/utils/api'

/** One governed resource: ceiling, usage, and whether a create would be refused. */
export interface QuotaResource {
  resource: string
  limit: number
  /** null = the live count could not be read; render as "unknown", never 0. */
  current: number | null
  exhausted: boolean
}

export interface QuotaOverview {
  tenantId: string
  resources: QuotaResource[]
  peakConcurrentLeases: number
  retryAfterSeconds: number
}

export const useQuotaStore = defineStore('quota', () => {
  // ─────────── State ───────────
  const overview = ref<QuotaOverview | null>(null)
  const loading = ref(false)
  const error = ref('')

  // ─────────── Actions ───────────

  /** Fetch the current tenant's quota overview. Errors are captured, not thrown:
   * quota visibility must never block the page that embeds the panel. */
  async function refresh(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const { data } = await api.get<QuotaOverview>('/quotas')
      overview.value = data
    } catch (err) {
      // Captured rather than surfaced: the panel is auxiliary, and the page
      // embedding it has its own error surface for primary content.
      error.value = (err as { response?: { data?: { message?: string } } })
        ?.response?.data?.message || 'Failed to load quota'
    } finally {
      loading.value = false
    }
  }

  /** Usage of one resource, or null when not yet loaded. */
  function usageOf(resource: string): QuotaResource | null {
    return overview.value?.resources.find((r) => r.resource === resource) || null
  }

  return { overview, loading, error, refresh, usageOf }
})
