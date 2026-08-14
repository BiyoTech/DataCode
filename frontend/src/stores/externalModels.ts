/**
 * External Models store — manages gateway/schema/table discovery and import.
 * Uses @/utils/api (axios instance, baseURL /api/v1).
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { useWorkspaceStore } from './workspace'
import api from '@/utils/api'
import { extractApiMessage, notifyError } from '@/utils/notifyError'

export interface GatewayInfo {
  name: string
  is_default: boolean
  /** Dialect of the gateway's connection (e.g. "postgres", "mysql", "duckdb").
   *  Drives the import dialog cascade depth: Postgres-family = 3-level
   *  (database → schema → tables), others = 2-level (database → tables). */
  dialect: string
}

export interface SchemaInfo {
  name: string
}

export interface TableInfo {
  schema_name: string
  table_name: string
  columns: { name: string; data_type: string }[]
}

export interface ImportResult {
  success: boolean
  written_files: string[]
  skipped_count: number
  success_count: number
  error?: string
}

export const useExternalModelsStore = defineStore('externalModels', () => {
  // ─────────── State ───────────
  const gateways = ref<GatewayInfo[]>([])
  const gatewaysLoading = ref(false)

  const schemas = ref<SchemaInfo[]>([])
  const schemasLoading = ref(false)

  const tables = ref<TableInfo[]>([])
  const tablesLoading = ref(false)

  const importLoading = ref(false)
  const importResult = ref<ImportResult | null>(null)
  const importError = ref<string | null>(null)

  // ─────────── Helpers ───────────
  function _wsPath(): string {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) throw new Error('No workspace selected')
    return ws.path
  }

  // ─────────── Actions ───────────

  /** List all gateways configured in the workspace config.yaml. */
  async function fetchGateways() {
    gatewaysLoading.value = true
    try {
      const { data } = await api.get('/gateways', {
        params: { workspace_path: _wsPath() },
      })
      gateways.value = data.gateways || []
    } catch (e: any) {
      notifyError(e, 'Failed to fetch gateways')
      gateways.value = []
    } finally {
      gatewaysLoading.value = false
    }
  }

  /** List databases/schemas accessible through a gateway (level-1).
   *
   *  For Postgres-family dialects this returns databases. For MySQL-family
   *  it also returns databases (MySQL has no schema sub-level). Use
   *  ``fetchSchemasInDatabase`` for the Postgres level-2 (schemas within a
   *  database). */
  async function fetchSchemas(gateway: string) {
    schemasLoading.value = true
    try {
      const { data } = await api.get('/schemas', {
        params: { workspace_path: _wsPath(), gateway },
      })
      schemas.value = data.schemas || []
    } catch (e: any) {
      notifyError(e, 'Failed to fetch schemas')
      schemas.value = []
    } finally {
      schemasLoading.value = false
    }
  }

  /** List schemas within a specific database (Postgres level-2 only).
   *
   *  Calls the same ``/schemas`` endpoint with the ``database`` query param.
   *  The backend ignores this param for non-Postgres dialects. */
  async function fetchSchemasInDatabase(gateway: string, database: string) {
    schemasLoading.value = true
    try {
      const { data } = await api.get('/schemas', {
        params: { workspace_path: _wsPath(), gateway, database },
      })
      schemas.value = data.schemas || []
    } catch (e: any) {
      notifyError(e, 'Failed to fetch schemas in database')
      schemas.value = []
    } finally {
      schemasLoading.value = false
    }
  }

  /** List tables in a schema (table name only, no columns). */
  async function fetchTables(gateway: string, schema: string) {
    tablesLoading.value = true
    try {
      const { data } = await api.post('/sqlmesh/schema/discover', {
        workspace_path: _wsPath(),
        gateway,
        schema_name: schema,
        table_name: '',
      })
      tables.value = (data.tables || []).map((t: any) => ({
        schema_name: t.schema_name,
        table_name: t.table_name,
        columns: t.columns || [],
      }))
    } catch (e: any) {
      notifyError(e, 'Failed to fetch tables')
      tables.value = []
    } finally {
      tablesLoading.value = false
    }
  }

  /** Execute the external models import. */
  async function importModels(payload: {
    gateway: string
    schema: string
    tables: string[]
    include: string
    exclude: string
    samples: number
    strict: boolean
  }): Promise<ImportResult> {
    importLoading.value = true
    importError.value = null
    importResult.value = null
    try {
      const { data } = await api.post('/external-models/import', {
        workspace_path: _wsPath(),
        ...payload,
      })
      importResult.value = data
      return data
    } catch (e: any) {
      importError.value = extractApiMessage(e, 'Import external models failed')
      notifyError(e, 'Import external models failed')
      throw e
    } finally {
      importLoading.value = false
    }
  }

  // ─────────── Reset ───────────
  function reset() {
    gateways.value = []
    schemas.value = []
    tables.value = []
    importResult.value = null
    importError.value = null
  }

  return {
    // State
    gateways, gatewaysLoading,
    schemas, schemasLoading,
    tables, tablesLoading,
    importLoading, importResult, importError,
    // Actions
    fetchGateways, fetchSchemas, fetchSchemasInDatabase, fetchTables, importModels,
    reset,
  }
})
