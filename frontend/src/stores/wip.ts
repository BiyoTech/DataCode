/**
 * WIP durability contract state (design 07 Q6 / ADR-003).
 *
 * Working copies live on replica-private storage, so the platform publishes a
 * concrete promise: a non-graceful crash loses at most one snapshot interval
 * of uncommitted work. That promise is only honest if the risk window is
 * VISIBLE — this store drives the three surfaces the contract requires:
 *
 * - the "last protected at" indicator (GitDetailPanel header);
 * - the degradation banner shown while a dirty copy is NOT protected;
 * - the recovery dialog offered when a snapshot is found on the Git server.
 *
 * Polling cadence is half the backend's snapshot interval (learned from the
 * status response), so the indicator never lags reality by more than one
 * half-interval. Everything hides while `enabled` is false (lease subsystem
 * off): showing an untruthful indicator is exactly the failure the contract
 * forbids.
 */
import { ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { MessagePlugin } from 'tdesign-vue-next'

import api from '@/utils/api'
import { extractApiMessage } from '@/utils/notifyError'
import { i18n } from '@/i18n'
import { useWorkspaceStore } from '@/stores/workspace'

/** A snapshot found on the Git server that the user has not decided on. */
export interface PendingSnapshot {
  ref: string
  commit: string
  /** UTC ISO-8601 of the snapshot commit; null when the backend could not read it. */
  taken_at: string | null
}

interface WipStatusResponse {
  enabled: boolean
  protected: boolean
  last_protected_at: string | null
  snapshot_interval_seconds: number
  pending_snapshot: PendingSnapshot | null
}

// Never poll faster than this even when the backend interval is tiny (dev).
const MIN_POLL_SECONDS = 30
const DEFAULT_INTERVAL_SECONDS = 600

export const useWipStore = defineStore('wip', () => {
  const workspaceStore = useWorkspaceStore()

  const isEnabled = ref(false)
  const isProtected = ref(true)
  const lastProtectedAt = ref<string | null>(null)
  const pendingSnapshot = ref<PendingSnapshot | null>(null)
  const snapshotIntervalSeconds = ref(DEFAULT_INTERVAL_SECONDS)

  const isRecovering = ref(false)
  const isDiscarding = ref(false)
  // Shared recovery dialog (AppShell renders it once).
  const dialogVisible = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null

  function stopPolling(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function schedulePolling(seconds: number): void {
    stopPolling()
    if (seconds <= 0) return
    timer = setInterval(() => {
      void refreshStatus()
    }, seconds * 1000)
  }

  async function refreshStatus(): Promise<void> {
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath) return
    try {
      const { data } = await api.get<WipStatusResponse>('/wip/status', {
        params: { workspace_path: wsPath },
      })
      isEnabled.value = data.enabled
      isProtected.value = data.protected
      lastProtectedAt.value = data.last_protected_at ?? null
      snapshotIntervalSeconds.value =
        data.snapshot_interval_seconds || DEFAULT_INTERVAL_SECONDS

      // Open the dialog only on the null -> pending transition: the pending
      // state persists until the user decides, and re-prompting on every poll
      // would train users to dismiss it — the failure the WIP design warns
      // about. The banner keeps a non-blocking entry point meanwhile.
      const hadPending = pendingSnapshot.value !== null
      pendingSnapshot.value = data.pending_snapshot ?? null
      if (pendingSnapshot.value && !hadPending) {
        dialogVisible.value = true
      }

      // Lease subsystem off: nothing to poll, the UI hides entirely.
      schedulePolling(
        data.enabled
          ? Math.max(MIN_POLL_SECONDS, Math.floor(snapshotIntervalSeconds.value / 2))
          : 0,
      )
    } catch {
      // Polling failures are silent by design: a 503 here means the lease
      // moved or the route is stale, both transient, and the existing timer
      // retries on the next tick. An error toast every 5 minutes would be
      // noise the user cannot act on.
    }
  }

  /** Restore the pending snapshot into the working copy (user decision). */
  async function recover(): Promise<void> {
    const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath || isRecovering.value) return
    isRecovering.value = true
    try {
      await api.post('/wip/recover', { workspace_path: wsPath })
      pendingSnapshot.value = null
      dialogVisible.value = false
      MessagePlugin.success(t('git.wip.recoverSuccess'))
      void refreshStatus()
    } catch (err: unknown) {
      // Notified but the dialog stays open: a 409 (dirty worktree / apply
      // conflict) is a state the user must resolve, not a reason to hide the
      // decision.
      MessagePlugin.error(extractApiMessage(err, t('git.wip.recoverFailed')))
    } finally {
      isRecovering.value = false
    }
  }

  /** Drop the pending snapshot from the Git server (user decision). */
  async function discard(): Promise<void> {
    const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath || isDiscarding.value) return
    isDiscarding.value = true
    try {
      await api.post('/wip/discard', { workspace_path: wsPath })
      pendingSnapshot.value = null
      dialogVisible.value = false
      MessagePlugin.success(t('git.wip.discardSuccess'))
      void refreshStatus()
    } catch (err: unknown) {
      MessagePlugin.error(extractApiMessage(err, t('git.wip.discardFailed')))
    } finally {
      isDiscarding.value = false
    }
  }

  /** Reset workspace-scoped state and stop polling (workspace/environment switch). */
  function reset(): void {
    stopPolling()
    isEnabled.value = false
    isProtected.value = true
    lastProtectedAt.value = null
    pendingSnapshot.value = null
    dialogVisible.value = false
    snapshotIntervalSeconds.value = DEFAULT_INTERVAL_SECONDS
  }

  // The working copy changes when the workspace OR the environment changes,
  // so the store keys off `currentWorkspace.path` (which the open/switch
  // flows update) rather than the workspace id.
  watch(
    () => workspaceStore.currentWorkspace?.path ?? null,
    (wsPath) => {
      reset()
      if (wsPath) void refreshStatus()
    },
    { immediate: true },
  )

  return {
    isEnabled,
    isProtected,
    lastProtectedAt,
    pendingSnapshot,
    snapshotIntervalSeconds,
    isRecovering,
    isDiscarding,
    dialogVisible,
    refreshStatus,
    recover,
    discard,
    reset,
  }
})
