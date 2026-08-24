/**
 * SQLMesh state-schema mismatch: detection + one-click repair.
 *
 * A workspace whose `config.yaml` declares a foreign `state_schema` is rejected
 * with 409 STATE_SCHEMA_MISMATCH on every plan/apply. The backend inlines the
 * structured fields (`gateway` / `expected` / `actual`) into the error body, so
 * this composable turns that payload into a shared repair surface:
 *
 * - `isStateSchemaMismatch` / `extractMismatchPayload` classify any axios error;
 * - `open` / `close` drive a single shared dialog (the Toolbar probe and the
 *   Plan panel both report the same 409 — one dialog, not two);
 * - `repair` rewrites the caller's config.yaml to the managed value on the
 *   backend and bumps `lastRepairedAt`, which owners of dependent state (the
 *   change-count probe, the plan panel) watch to refresh themselves.
 */
import { ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'

import api from '@/utils/api'
import { extractApiMessage } from '@/utils/notifyError'
import { i18n } from '@/i18n'

/** Structured fields inlined into the 409 body by StateSchemaMismatchError. */
export interface StateSchemaMismatchPayload {
  error_code?: string
  message?: string
  gateway?: string
  expected?: string
  actual?: string
  config_file?: string
}

/** Whether an axios error is the state-schema mismatch 409. */
export function isStateSchemaMismatch(err: unknown): boolean {
  return extractMismatchPayload(err)?.error_code === 'STATE_SCHEMA_MISMATCH'
}

/** Pull the structured mismatch fields out of an axios error (or any object). */
export function extractMismatchPayload(err: unknown): StateSchemaMismatchPayload | null {
  const payload = (err as { response?: { data?: StateSchemaMismatchPayload } })?.response?.data
  return payload && typeof payload === 'object' ? payload : null
}

// Module-level singleton: every component reporting or repairing the same
// mismatch shares one dialog and one "last repaired" timestamp.
const payload = ref<StateSchemaMismatchPayload | null>(null)
const visible = ref(false)
const repairing = ref(false)
const lastRepairedAt = ref(0)

export function useStateSchemaRepair() {
  /**
   * Rewrite the working copy's config.yaml to the managed state schema.
   * Returns whether the repair succeeded; toasts the outcome either way so
   * both call sites (dialog confirm, panel retry button) stay thin.
   */
  async function repair(workspacePath: string): Promise<boolean> {
    const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
    repairing.value = true
    try {
      const { data } = await api.post('/sqlmesh/config/state-schema/repair', {
        workspace_path: workspacePath,
      })
      lastRepairedAt.value = Date.now()
      MessagePlugin.success(
        t('plan.stateSchema.repairSuccess', { schema: data?.state_schema ?? '' })
      )
      return true
    } catch (err: unknown) {
      MessagePlugin.error(extractApiMessage(err, t('plan.stateSchema.repairFailed')))
      return false
    } finally {
      repairing.value = false
    }
  }

  /** Open the shared dialog for a detected mismatch payload. */
  function open(p: StateSchemaMismatchPayload | null): void {
    if (!p) return
    payload.value = p
    visible.value = true
  }

  function close(): void {
    visible.value = false
  }

  return { payload, visible, repairing, lastRepairedAt, open, close, repair }
}
