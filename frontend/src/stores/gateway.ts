/**
 * Gateway Store - global source of truth for the currently selected SQLMesh gateway.
 *
 * The selected gateway drives list-level SQLMesh operations (model list,
 * full-graph lineage, plan, apply, cancel, changes count). Single-model
 * operations (detail / preview / column lineage) keep resolving the gateway
 * from the model itself (see models store `_gatewayFor`).
 *
 * Selection is persisted per workspace in localStorage so switching workspaces
 * does not leak a gateway that does not exist in the other workspace's config.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useWorkspaceStore } from './workspace'
import type { GatewayInfo } from './externalModels'
import api from '@/utils/api'
import { extractApiMessage } from '@/utils/notifyError'

// localStorage key prefix; the concrete key is `${STORAGE_PREFIX}${workspaceId}`.
const STORAGE_PREFIX = 'sqlmesh_gateway:'

export const useGatewayStore = defineStore('gateway', () => {
  // ─────────── State ───────────
  const gateways = ref<GatewayInfo[]>([])
  const gatewaysLoading = ref(false)
  // Empty string means "use config.yaml default gateway" (backend falls back).
  const selectedGateway = ref('')
  const defaultGateway = ref('')

  // Connectivity of the currently selected gateway's warehouse.
  // null = unknown/not probed yet, true = reachable, false = unreachable.
  // This is decoupled from workspace/file loading: it is only probed on
  // explicit gateway-dependent actions (switch gateway, select environment,
  // refresh changes) and surfaced as a local hint, never a global toast.
  const connectionOk = ref<boolean | null>(null)
  const connectionError = ref('')
  const pinging = ref(false)

  // ─────────── Computed ───────────
  // The gateway actually in effect: explicit selection, else backend default.
  const currentGateway = computed(() => selectedGateway.value || defaultGateway.value || '')

  // ─────────── Helpers ───────────
  function _storageKey(): string | null {
    const wsId = useWorkspaceStore().currentWorkspaceId
    return wsId ? `${STORAGE_PREFIX}${wsId}` : null
  }

  function _readPersisted(): string | null {
    const key = _storageKey()
    return key ? localStorage.getItem(key) : null
  }

  function _persist(gateway: string): void {
    const key = _storageKey()
    if (!key) return
    if (gateway) {
      localStorage.setItem(key, gateway)
    } else {
      localStorage.removeItem(key)
    }
  }

  // ─────────── Actions ───────────

  /**
   * Load gateways for the current workspace and initialize the selection.
   *
   * Priority: persisted selection (if still valid) → backend default_gateway
   * → first gateway in the list. Invalid persisted values are discarded to
   * avoid targeting a gateway that no longer exists in config.yaml.
   */
  async function fetchGateways(): Promise<void> {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) {
      gateways.value = []
      return
    }

    gatewaysLoading.value = true
    try {
      const { data } = await api.get('/gateways', {
        params: { workspace_path: ws.path },
      })
      gateways.value = (data.gateways || []) as GatewayInfo[]
      defaultGateway.value = data.default_gateway || ''

      const names = new Set(gateways.value.map((g) => g.name))
      const persisted = _readPersisted()

      if (persisted && names.has(persisted)) {
        selectedGateway.value = persisted
      } else {
        // Fall back to backend default, then first available gateway.
        selectedGateway.value =
          defaultGateway.value && names.has(defaultGateway.value)
            ? defaultGateway.value
            : gateways.value[0]?.name || ''
        _persist(selectedGateway.value)
      }
    } catch (e: any) {
      // Do not raise a global toast here: gateway listing must not block
      // workspace/file loading. Failures (e.g. missing/invalid config.yaml on
      // a freshly created workspace) are logged and leave an empty list.
      console.error('[gateway] failed to fetch gateways', e)
      gateways.value = []
      defaultGateway.value = ''
      selectedGateway.value = ''
    } finally {
      gatewaysLoading.value = false
    }
  }

  /**
   * Probe connectivity of the currently effective gateway's warehouse.
   *
   * Lightweight, short-timeout check via `GET /gateways/ping`. Updates
   * `connectionOk` / `connectionError` and returns the boolean result so
   * callers can decide whether to proceed with a DB-dependent action.
   * Never throws and never triggers a global toast.
   */
  async function pingGateway(): Promise<boolean> {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) {
      connectionOk.value = null
      connectionError.value = ''
      return false
    }

    pinging.value = true
    try {
      const { data } = await api.get('/gateways/ping', {
        params: {
          workspace_path: ws.path,
          gateway: currentGateway.value || undefined,
        },
      })
      connectionOk.value = !!data?.ok
      connectionError.value = data?.ok ? '' : String(data?.error || 'Gateway connection failed')
      return connectionOk.value
    } catch (e: any) {
      // Network/unexpected error: treat as unreachable, keep it local.
      connectionOk.value = false
      connectionError.value = extractApiMessage(e, 'Gateway connection failed')
      return false
    } finally {
      pinging.value = false
    }
  }

  /** Update the selected gateway and persist it for the current workspace. */
  function setGateway(gateway: string): void {
    selectedGateway.value = gateway
    // Reset connectivity state; the caller re-probes for the new gateway.
    connectionOk.value = null
    connectionError.value = ''
    _persist(gateway)
  }

  /** Clear all gateway state (used on workspace switch before reloading). */
  function reset(): void {
    gateways.value = []
    gatewaysLoading.value = false
    selectedGateway.value = ''
    defaultGateway.value = ''
    connectionOk.value = null
    connectionError.value = ''
    pinging.value = false
  }

  return {
    // State
    gateways,
    gatewaysLoading,
    selectedGateway,
    defaultGateway,
    connectionOk,
    connectionError,
    pinging,
    // Computed
    currentGateway,
    // Actions
    fetchGateways,
    pingGateway,
    setGateway,
    reset,
  }
})
