/**
 * Workspace log store — history pages plus a live tail.
 *
 * The backend writes every workspace-attributable log line as NDJSON under
 * `{workspace}/logs/`. This store reads the newest page on open, then keeps a
 * Server-Sent Events tail appending to the same buffer, so the panel behaves
 * like `tail -f` without the user choosing between "recent" and "live".
 *
 * The buffer is a ring: logs are unbounded by nature and a browser tab left
 * open for a day must not grow without limit.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { useEventStream } from '@/composables/useEventStream'
import { useWorkspaceStore } from '@/stores/workspace'
import { getAccessToken } from '@/utils/authSession'
import { notifyError } from '@/utils/notifyError'

/** Upper bound on retained lines. Older lines are dropped from the front. */
const MAX_BUFFERED_LINES = 5000

export type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical'

export interface LogEntry {
  /** Stable key: `{source}:{file}:{line}`. */
  id: string
  /** ISO 8601 UTC timestamp; empty for text lines with no parseable time. */
  ts: string
  level: LogLevel
  /** File origin, e.g. `sqlmesh-service` or `sqlmesh-cli`. */
  source: string
  service?: string
  /** Event name for structured lines, raw text for external logs. */
  event: string
  trace_id?: string
  environment?: string
  operation?: string
  error_code?: string
  error_log_id?: string
  logger?: string
  fields?: Record<string, unknown>
}

export interface ArchivedErrorSummary {
  error_id: string
  error_code: string
  message: string
  operation: string
  occurred_at_utc: string
  service: string
}

export const useLogsStore = defineStore('logs', () => {
  const workspaceStore = useWorkspaceStore()

  const entries = ref<LogEntry[]>([])
  const sources = ref<string[]>([])
  const archivedErrors = ref<ArchivedErrorSummary[]>([])

  const loading = ref(false)
  const loadingMore = ref(false)
  const nextCursor = ref<string | null>(null)

  // Filters are applied server-side for history and client-side for the tail,
  // so changing one re-fetches rather than filtering a partial buffer.
  const selectedSources = ref<string[]>([])
  const selectedLevels = ref<LogLevel[]>([])
  const search = ref('')

  const paused = ref(false)
  const streaming = ref(false)

  const hasMore = computed(() => nextCursor.value !== null)

  function workspacePath(): string {
    return workspaceStore.currentWorkspace?.path ?? ''
  }

  function filterParams(): Record<string, string> {
    const params: Record<string, string> = { workspace_path: workspacePath() }
    if (selectedSources.value.length) params.sources = selectedSources.value.join(',')
    if (selectedLevels.value.length) params.levels = selectedLevels.value.join(',')
    if (search.value.trim()) params.q = search.value.trim()
    return params
  }

  /** Insert entries oldest-first, dropping the front once the ring is full. */
  function append(incoming: LogEntry[]) {
    if (!incoming.length) return
    const known = new Set(entries.value.map((entry) => entry.id))
    const fresh = incoming.filter((entry) => !known.has(entry.id))
    if (!fresh.length) return

    entries.value = entries.value.concat(fresh)
    if (entries.value.length > MAX_BUFFERED_LINES) {
      entries.value = entries.value.slice(entries.value.length - MAX_BUFFERED_LINES)
    }
  }

  /** Load the newest page, replacing whatever is buffered. */
  async function load() {
    if (!workspacePath()) return
    loading.value = true
    try {
      const { data } = await api.get('/logs', { params: filterParams() })
      // The API answers newest-first; the panel reads top-to-bottom in time.
      entries.value = (data.entries ?? []).slice().reverse()
      sources.value = data.sources ?? []
      nextCursor.value = data.next_cursor ?? null
    } catch (error) {
      notifyError(error, 'Failed to load workspace logs')
    } finally {
      loading.value = false
    }
  }

  /** Prepend the next older page. */
  async function loadMore() {
    if (!nextCursor.value || loadingMore.value) return
    loadingMore.value = true
    try {
      const { data } = await api.get('/logs', {
        params: { ...filterParams(), cursor: nextCursor.value },
      })
      const older = (data.entries ?? []).slice().reverse()
      entries.value = older.concat(entries.value).slice(-MAX_BUFFERED_LINES)
      nextCursor.value = data.next_cursor ?? null
    } catch (error) {
      notifyError(error, 'Failed to load older log entries')
    } finally {
      loadingMore.value = false
    }
  }

  async function loadArchivedErrors() {
    if (!workspacePath()) return
    try {
      const { data } = await api.get('/logs/errors', {
        params: { workspace_path: workspacePath() },
      })
      archivedErrors.value = data.errors ?? []
    } catch (error) {
      notifyError(error, 'Failed to load the error archive')
    }
  }

  // The SSE endpoint is proxied by the gateway, which authenticates from the
  // bearer header the stream composable attaches.
  const stream = useEventStream({
    url: () => {
      const params = new URLSearchParams(filterParams())
      return `/api/v1/logs/stream?${params.toString()}`
    },
    onMessage: (_eventType, rawData) => {
      if (paused.value) return
      try {
        append([JSON.parse(rawData) as LogEntry])
      } catch {
        // A malformed line is not worth interrupting the stream for.
      }
    },
    onStateChange: (state) => {
      streaming.value = state === 'connected'
    },
    label: 'LOG',
  })

  /** Open the panel: newest page first, then follow. */
  async function start() {
    if (!workspacePath() || !getAccessToken()) return
    await load()
    stream.connect()
  }

  function stop() {
    stream.disconnect()
    streaming.value = false
  }

  /** Re-apply filters: the tail must be reopened so the server filters too. */
  async function applyFilters() {
    stop()
    await start()
  }

  function togglePause() {
    paused.value = !paused.value
  }

  function clear() {
    entries.value = []
    nextCursor.value = null
  }

  function reset() {
    stop()
    entries.value = []
    sources.value = []
    archivedErrors.value = []
    nextCursor.value = null
    selectedSources.value = []
    selectedLevels.value = []
    search.value = ''
    paused.value = false
  }

  return {
    entries,
    sources,
    archivedErrors,
    loading,
    loadingMore,
    hasMore,
    selectedSources,
    selectedLevels,
    search,
    paused,
    streaming,
    connectionState: stream.connectionState,
    load,
    loadMore,
    loadArchivedErrors,
    start,
    stop,
    applyFilters,
    togglePause,
    clear,
    reset,
  }
})
