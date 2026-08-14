/**
 * Ad-hoc analysis store — powers the ad-hoc SQL workbench.
 *
 * It owns three concerns:
 *   1. A project-space data-catalog tree built from the workspace's SQLMesh
 *      models (grouped by FQN segments: catalog -> schema -> table). Only the
 *      tables that exist in the current project are listed, mirroring the
 *      ModelTree data source (`/sqlmesh/models`).
 *   2. Lazily-loaded table columns (name / type / comment) fetched on expand,
 *      plus single-table row preview via the model evaluate endpoint.
 *   3. Ad-hoc SQL execution against the active gateway.
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { useWorkspaceStore } from './workspace'
import { useModelsStore } from './models'
import api from '@/utils/api'
import { extractApiMessage, notifyError } from '@/utils/notifyError'
import type { GatewayInfo } from './externalModels'
import type { ModelSummary } from '@/types/models'

export type CatalogNodeType = 'folder' | 'table' | 'column'

// A single node in the project-space data-catalog tree.
//   folder : intermediate FQN segment (catalog/schema), expandable
//   table  : queryable model leaf, expandable to show its columns lazily
//   column : a table column leaf carrying its data type and comment
export interface CatalogNode {
  key: string // unique dot-path key, e.g. "sales" or "sales.orders"
  name: string // display name (last segment)
  type: CatalogNodeType
  fqn?: string // table: full FQN used in FROM clauses
  modelName?: string // table: model name used for detail / evaluate
  comment?: string // table/column: description shown on hover
  dataType?: string // column: data type label
  loaded?: boolean // table: whether columns have been fetched
  loading?: boolean // table: columns fetch in-flight
  children: CatalogNode[]
}

// A persisted ad-hoc SQL file stored under the {workspace}/ad-hocs/ directory.
export interface AdhocFile {
  path: string // e.g. "ad-hocs/query1.sql"
  name: string // e.g. "query1.sql"
  displayName: string // e.g. "query1" (stripped .sql extension)
}

// Normalized ad-hoc query / preview result consumed by the workbench grids.
export interface QueryResult {
  columns: string[]
  rows: string[][]
  totalRows: number
  execTimeMs: number
}

export const useAdhocStore = defineStore('adhoc', () => {
  const modelsStore = useModelsStore()

  // ─────────── Gateway state (SQL execution only) ───────────
  const gateways = ref<GatewayInfo[]>([])
  const gateway = ref<string>('') // active gateway ('' = default)

  // ─────────── Catalog state (project models) ───────────
  const tree = ref<CatalogNode[]>([])
  const catalogLoading = ref(false)
  const catalogError = ref('')

  // ─────────── Ad-hoc file management state ───────────
  const adhocFiles = ref<AdhocFile[]>([])
  const filesLoading = ref(false)
  const filesError = ref('')
  const ADHOC_DIR = 'ad-hocs'

  function _wsPath(): string {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) throw new Error('No workspace selected')
    return ws.path
  }

  /** Resolve the current workspace path, or null when none is selected.
   *  Used by passive loaders that run automatically on mount — after a full
   *  page reload the workspace list is fetched asynchronously, so
   *  `currentWorkspace` is transiently null and must not raise. */
  function _wsPathOrNull(): string | null {
    return useWorkspaceStore().currentWorkspace?.path ?? null
  }

  /** Load configured gateways and pick the default (or first) as active. */
  async function fetchGateways() {
    const wsPath = _wsPathOrNull()
    if (!wsPath) {
      gateways.value = []
      gateway.value = ''
      return
    }
    try {
      const { data } = await api.get('/gateways', {
        params: { workspace_path: wsPath },
      })
      gateways.value = data.gateways || []
      const def = gateways.value.find((g) => g.is_default) || gateways.value[0]
      gateway.value = def ? def.name : ''
    } catch (e) {
      notifyError(e, 'Failed to fetch gateways')
      gateways.value = []
    }
  }

  /** Load the project-space model catalog (the queryable tables). */
  async function loadCatalog() {
    if (!_wsPathOrNull()) {
      tree.value = []
      catalogError.value = ''
      return
    }
    catalogLoading.value = true
    catalogError.value = ''
    try {
      await modelsStore.fetchModels()
      tree.value = _buildCatalogTree(modelsStore.models)
    } catch (e: any) {
      catalogError.value = extractApiMessage(e, 'Failed to load catalog')
      tree.value = []
    } finally {
      catalogLoading.value = false
    }
  }

  /** Reload the whole catalog from scratch. */
  async function refreshCatalog() {
    await loadCatalog()
  }

  /** Lazily load a table's columns on first expand. */
  async function loadColumns(node: CatalogNode) {
    if (node.type !== 'table' || node.loaded || node.loading || !node.modelName) return
    node.loading = true
    try {
      const { data } = await api.get(`/sqlmesh/models/${encodeURIComponent(node.modelName)}`, {
        params: { workspace_path: _wsPath() },
      })
      node.children = (data.columns || []).map((c: any) => ({
        key: `${node.key}::${c.name}`,
        name: c.name,
        type: 'column' as const,
        dataType: c.type || '',
        comment: c.description || '',
        children: [],
      }))
      // Backfill the table comment from detail when the summary lacked one.
      if (!node.comment) node.comment = data.description || ''
      node.loaded = true
    } catch (e) {
      notifyError(e, 'Failed to load columns')
      node.children = []
    } finally {
      node.loading = false
    }
  }

  // ─────────── Ad-hoc file management ───────────

  /** List SQL files under the ad-hocs/ directory. */
  async function listAdhocFiles() {
    const wsPath = _wsPathOrNull()
    if (!wsPath) {
      adhocFiles.value = []
      filesError.value = ''
      return
    }
    filesLoading.value = true
    filesError.value = ''
    try {
      const { data } = await api.get('/files/list', {
        params: {
          workspace_path: wsPath,
          directory: ADHOC_DIR,
          recursive: false,
        },
      })
      adhocFiles.value = (data.files || [])
        .filter((f: any) => !f.is_directory && f.path.endsWith('.sql'))
        .map((f: any) => {
          const name = f.path.split('/').pop() || f.path
          return {
            path: f.path,
            name,
            displayName: name.replace(/\.sql$/i, ''),
          } as AdhocFile
        })
    } catch (e: any) {
      filesError.value = extractApiMessage(e, 'Failed to list files')
      notifyError(e, 'Failed to list ad-hoc files')
      // Directory may not exist yet — that's fine, return empty
      if (e?.response?.status === 404) {
        adhocFiles.value = []
        filesError.value = ''
      }
    } finally {
      filesLoading.value = false
    }
  }

  /** Load content of an ad-hoc SQL file. */
  async function loadAdhocFile(filePath: string): Promise<string> {
    const { data } = await api.get('/files/read', {
      params: { workspace_path: _wsPath(), file_path: filePath },
    })
    return data.content || ''
  }

  /** Save (write) an ad-hoc SQL file. Creates parent directory implicitly. */
  async function saveAdhocFile(filePath: string, content: string) {
    await api.post('/files/write', {
      workspace_path: _wsPath(),
      file_path: filePath,
      content,
    })
  }

  /** Create a new ad-hoc query file with empty content. If a file with the
   *  proposed name already exists, automatically appends `-2`, `-3`, ... to
   *  disambiguate. Returns the actual file path, the final (possibly
   *  disambiguated) name, and a flag indicating whether the name was changed. */
  async function createAdhocQuery(name: string): Promise<{
    filePath: string
    finalName: string
    renamed: boolean
  }> {
    const baseName = name.replace(/\.sql$/i, '') // strip any user-typed extension
    const { finalName, renamed } = findAvailableFileName(baseName)
    const filePath = `${ADHOC_DIR}/${finalName}.sql`
    // Ensure the ad-hocs directory exists first
    try {
      await api.post('/files/mkdir', {
        workspace_path: _wsPath(),
        directory: ADHOC_DIR,
      })
    } catch {
      // Directory may already exist — ignore
    }
    await api.post('/files/write', {
      workspace_path: _wsPath(),
      file_path: filePath,
      content: '',
    })
    // Refresh the file list eagerly so subsequent conflict checks see the new file
    await listAdhocFiles()
    return { filePath, finalName, renamed }
  }

  /** Return a non-conflicting base name and whether it was auto-suffixed. */
  function findAvailableFileName(baseName: string): {
    finalName: string
    renamed: boolean
  } {
    const taken = new Set(adhocFiles.value.map((f) => f.displayName))
    if (!taken.has(baseName)) return { finalName: baseName, renamed: false }
    let n = 2
    while (taken.has(`${baseName}-${n}`)) n += 1
    return { finalName: `${baseName}-${n}`, renamed: true }
  }

  /** Rename an ad-hoc file (oldPath → newPath, both relative to workspace). */
  async function renameAdhocFile(oldPath: string, newPath: string) {
    await api.post('/files/rename', {
      workspace_path: _wsPath(),
      old_path: oldPath,
      new_path: newPath,
    })
  }

  /** Delete an ad-hoc file. */
  async function deleteAdhocFile(filePath: string) {
    await api.post('/files/delete', {
      workspace_path: _wsPath(),
      file_path: filePath,
    })
  }

  // ─────────── Query / preview execution ───────────

  /** Execute an ad-hoc SQL statement and return a normalized result. */
  async function runQuery(sql: string, rowLimit = 1000): Promise<QueryResult> {
    const { data } = await api.post('/sqlmesh/query', {
      workspace_path: _wsPath(),
      sql,
      gateway: gateway.value || undefined,
      row_limit: rowLimit,
    })
    return _normalizeResult(data)
  }

  /** Preview a limited number of rows for a table by evaluating its model. */
  async function previewTable(node: CatalogNode, limit = 10): Promise<QueryResult> {
    if (node.type !== 'table' || !node.modelName) {
      throw new Error('Not a previewable table')
    }
    const { data } = await api.post('/sqlmesh/evaluate', {
      workspace_path: _wsPath(),
      model: node.modelName,
      gateway: gateway.value || undefined,
      limit,
    })
    return _normalizeResult(data)
  }

  // ─────────── Private helpers ───────────

  /** Normalize a backend query/evaluate payload into a QueryResult. */
  function _normalizeResult(data: any): QueryResult {
    return {
      columns: data.columns || [],
      rows: data.rows || [],
      totalRows: data.total_rows ?? (data.rows?.length || 0),
      execTimeMs: data.execution_time_ms ?? 0,
    }
  }

  /**
   * Build a hierarchical tree from model FQNs (e.g. `"catalog"."schema"."table"`).
   * Quoted identifier segments are unquoted for display; intermediate segments
   * become folder nodes; the final segment becomes a queryable table leaf.
   *
   * `modelName` keeps the ORIGINAL (possibly quoted) name because the backend
   * `get_model` lookup expects it verbatim; `fqn`/`name`/`key` use the
   * unquoted forms for display and SQL assembly.
   */
  function _buildCatalogTree(models: ModelSummary[]): CatalogNode[] {
    const root: CatalogNode[] = []
    for (const model of models) {
      const parts = _parseFqn(model.fqn || model.name)
      if (parts.length === 0) continue

      let children = root
      for (let i = 0; i < parts.length; i++) {
        const segment = parts[i]
        const isLeaf = i === parts.length - 1
        const key = parts.slice(0, i + 1).join('.')

        let node = children.find(
          (n) => n.name === segment && n.type === (isLeaf ? 'table' : 'folder'),
        )
        if (!node) {
          node = {
            key,
            name: segment,
            type: isLeaf ? 'table' : 'folder',
            ...(isLeaf
              ? {
                  fqn: parts.join('.'),
                  modelName: model.name,
                  comment: model.description || '',
                  loaded: false,
                  loading: false,
                }
              : {}),
            children: [],
          }
          children.push(node)
        }
        if (!isLeaf) children = node.children
      }
    }
    _sortTree(root)
    return root
  }

  /**
   * Split an FQN into clean (unquoted) segments. Handles SQL standard
   * double-quoted identifiers (e.g. `"a"."b"`, `"a.b"."c"`, `""""` escapes).
   * Bare (unquoted) dotted names are also supported.
   */
  function _parseFqn(fqn: string): string[] {
    const parts: string[] = []
    const re = /"[^"]*(?:""[^"]*)*"|[^.]+/g
    let m: RegExpExecArray | null
    while ((m = re.exec(fqn)) !== null) {
      parts.push(_stripQuotes(m[0]))
    }
    return parts.filter(Boolean)
  }

  /** Remove surrounding double quotes from a quoted SQL identifier, resolving
   * doubled-quote escapes (`""` -> `"`). Bare names pass through unchanged. */
  function _stripQuotes(s: string): string {
    if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
      return s.slice(1, -1).replace(/""/g, '"')
    }
    return s
  }

  /** Recursively sort tree nodes: folders first, then alphabetical. */
  function _sortTree(nodes: CatalogNode[]): void {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'folder' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    for (const node of nodes) _sortTree(node.children)
  }

  /** Clear workspace-scoped ad-hoc state (used on workspace switch). */
  function reset() {
    gateways.value = []
    gateway.value = ''
    tree.value = []
    catalogLoading.value = false
    catalogError.value = ''
    adhocFiles.value = []
    filesLoading.value = false
    filesError.value = ''
  }

  return {
    // Gateway state
    gateways,
    gateway,
    // Catalog state
    tree,
    catalogLoading,
    catalogError,
    // File management state
    adhocFiles,
    filesLoading,
    filesError,
    // Actions
    fetchGateways,
    loadCatalog,
    refreshCatalog,
    loadColumns,
    listAdhocFiles,
    loadAdhocFile,
    saveAdhocFile,
    createAdhocQuery,
    renameAdhocFile,
    deleteAdhocFile,
    findAvailableFileName,
    runQuery,
    previewTable,
    reset,
  }
})
