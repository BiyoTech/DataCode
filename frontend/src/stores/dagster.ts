/**
 * Dagster store — manages Asset Dag state for the active workspace.
 *
 * Responsibilities:
 * - Fetch the deployment mode (local/remote) + subprocess health.
 * - Ensure the workspace Code Location gRPC server is running, then load the
 *   asset dependency graph for DAG rendering.
 * - List/trigger runs and poll their status; list schedules.
 *
 * All data flows through the Gateway (`/api/v1/dagster/*`). Timestamps returned
 * by the API are UTC epoch seconds; components convert them for display.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '@/utils/api'
import { notifyError } from '@/utils/notifyError'
import { useEnvironmentStore } from './environment'
import type {
  AssetDagGraph,
  AssetInfo,
  CodeLocationInfo,
  ModeInfo,
  RunInfo,
  RunLogEvent,
  ScheduleInfo,
  SqlmeshLineageResponse,
} from '@/types/dagster'

// Prefix applied to every SQLMesh-derived Dagster asset key (mirrors the
// backend bridge). Used to detect SQLMesh assets and to convert between an
// asset key string and a SQLMesh model display name.
const SQLMESH_PREFIX = 'sqlmesh'

/** Whether an asset key string denotes a SQLMesh-bridged model. */
function isSqlmeshAssetKey(keyStr: string): boolean {
  return keyStr === SQLMESH_PREFIX || keyStr.startsWith(`${SQLMESH_PREFIX}__`)
}

/**
 * Convert a SQLMesh asset key string (``sqlmesh__schema__table``) into the
 * model display name (``schema.table``) expected by /sqlmesh/lineage.
 */
function sqlmeshAssetKeyToModelName(keyStr: string): string {
  const withoutPrefix = keyStr.startsWith(`${SQLMESH_PREFIX}__`)
    ? keyStr.slice(SQLMESH_PREFIX.length + 2)
    : keyStr
  return withoutPrefix.split('__').join('.')
}

/**
 * Convert a SQLMesh model display name (``schema.table``) into its Dagster
 * asset key string (``sqlmesh__schema__table``). Mirrors the backend bridge so
 * lineage nodes line up with the code-location asset keys.
 */
function sqlmeshModelNameToAssetKey(name: string): string {
  const parts = name
    .split('.')
    .map((p) => p.replace(/"/g, ''))
    .filter(Boolean)
  return [SQLMESH_PREFIX, ...parts].join('__')
}

export const useDagsterStore = defineStore('dagster', () => {
  // ─────────── State ───────────
  const mode = ref<ModeInfo | null>(null)
  // Full asset inventory for the workspace. Drives the left list + native
  // subgraph computation; NOT rendered directly on the canvas.
  const graph = ref<AssetDagGraph>({ nodes: [], edges: [] })
  // Subgraph shown on the canvas for the currently selected asset. Empty until
  // an asset is selected (the canvas shows an empty-state hint meanwhile).
  const displayGraph = ref<AssetDagGraph>({ nodes: [], edges: [] })
  const selectedAssetKey = ref<string | null>(null)
  const runs = ref<RunInfo[]>([])
  const runLogs = ref<Record<string, RunLogEvent[]>>({})
  const schedules = ref<ScheduleInfo[]>([])

  const graphLoading = ref(false)
  // Loading / error state for the selected asset's lineage subgraph.
  const lineageLoading = ref(false)
  const lineageError = ref('')
  const runsLoading = ref(false)
  const schedulesLoading = ref(false)
  const errorMsg = ref('')

  // Workspace whose graph is currently loaded (avoids redundant reloads).
  let loadedWorkspacePath: string | null = null
  // Environment backing the currently loaded graph; a change forces a reload
  // so the DAG reflects the selected SQLMesh virtual environment's topology.
  let loadedEnvironment: string | null = null
  // Workspace path backing the full inventory; needed to fetch SQLMesh lineage.
  let currentWorkspacePath = ''

  // ─────────── Getters ───────────
  // Selected asset metadata is resolved from the full inventory so the detail
  // panel keeps working regardless of the (possibly focused) display subgraph.
  const selectedAsset = computed<AssetInfo | null>(
    () => graph.value.nodes.find((n) => n.asset_key_str === selectedAssetKey.value) || null,
  )

  // Upstream / downstream lists are derived from the display subgraph so they
  // reflect the same lineage the canvas renders (native + SQLMesh alike).
  const upstream = computed<string[]>(() => {
    const key = selectedAssetKey.value
    if (!key) return []
    return displayGraph.value.edges.filter((e) => e.target === key).map((e) => e.source)
  })

  const downstream = computed<string[]>(() => {
    const key = selectedAssetKey.value
    if (!key) return []
    return displayGraph.value.edges.filter((e) => e.source === key).map((e) => e.target)
  })

  const isRemote = computed(() => mode.value?.mode === 'remote')

  /**
   * Resolve an asset key string to a human-friendly display name. Looks up the
   * node (preferring the display subgraph, then the full inventory) and returns
   * its ``name`` (SQLMesh model ``schema.table``) when present, else the key.
   */
  function displayNameFor(key: string): string {
    const node =
      displayGraph.value.nodes.find((n) => n.asset_key_str === key) ||
      graph.value.nodes.find((n) => n.asset_key_str === key)
    return node?.name || key
  }

  // ─────────── Actions ───────────

  /** Fetch the active Dagster mode + endpoint info. Safe to call repeatedly. */
  async function fetchMode(): Promise<void> {
    try {
      const { data } = await api.get<ModeInfo>('/dagster/mode')
      mode.value = data
    } catch (e) {
      // Mode is informational; never block the view on it.
      console.warn('[dagster] Failed to fetch mode:', e)
    }
  }

  /**
   * Ensure the workspace Code Location is running, then load the full asset
   * inventory (left list + native subgraph source). The canvas stays empty
   * until the user selects an asset.
   *
   * Cached per (workspace path, environment); pass force=true to bypass the
   * cache (e.g. after a Code Location reload). The graph is scoped to the
   * selected SQLMesh virtual environment so switching environments re-fetches.
   */
  async function loadGraph(workspacePath: string, force = false): Promise<void> {
    if (!workspacePath) return
    currentWorkspacePath = workspacePath
    const environment = useEnvironmentStore().currentEnvironment
    if (!force && loadedWorkspacePath === workspacePath && loadedEnvironment === environment) {
      return
    }

    graphLoading.value = true
    errorMsg.value = ''
    try {
      // The gRPC Code Location must exist before the graph can resolve.
      await api
        .post('/dagster/pipelines/code-location/ensure', { workspace_path: workspacePath })
        .catch((e) => {
          // Log but continue — the graph query may still return instance-wide
          // assets even if this workspace has no assets yet.
          console.warn('[dagster] ensure code-location failed:', e)
        })

      const { data } = await api.get<AssetDagGraph>('/dagster/assets/graph', {
        params: { workspace_path: workspacePath, environment },
      })
      graph.value = { nodes: data.nodes || [], edges: data.edges || [] }
      loadedWorkspacePath = workspacePath
      loadedEnvironment = environment
    } catch (e) {
      errorMsg.value = 'Failed to load asset graph'
      notifyError(e, errorMsg.value)
    } finally {
      graphLoading.value = false
    }
  }

  /** Reload the workspace Code Location (after user edits asset-dag/ code). */
  async function reloadCodeLocation(workspacePath: string): Promise<void> {
    await api.post('/dagster/pipelines/code-location/reload', { workspace_path: workspacePath })
    await loadGraph(workspacePath, true)
  }

  /**
   * Build the up/down-stream subgraph for a native Dagster asset by traversing
   * the full inventory graph locally (BFS over both edge directions). Runs only
   * on selection change, so the O(V+E) cost is negligible.
   */
  function buildNativeSubgraph(key: string): AssetDagGraph {
    const edges = graph.value.edges
    // Adjacency maps for both directions.
    const upAdj = new Map<string, string[]>() // node -> upstream sources
    const downAdj = new Map<string, string[]>() // node -> downstream targets
    for (const e of edges) {
      if (!downAdj.has(e.source)) downAdj.set(e.source, [])
      downAdj.get(e.source)!.push(e.target)
      if (!upAdj.has(e.target)) upAdj.set(e.target, [])
      upAdj.get(e.target)!.push(e.source)
    }

    const included = new Set<string>([key])
    // Walk all ancestors (upstream) and descendants (downstream).
    const walk = (start: string, adj: Map<string, string[]>) => {
      const queue = [start]
      const seen = new Set<string>([start])
      while (queue.length) {
        const cur = queue.shift()!
        for (const next of adj.get(cur) ?? []) {
          if (seen.has(next)) continue
          seen.add(next)
          included.add(next)
          queue.push(next)
        }
      }
    }
    walk(key, upAdj)
    walk(key, downAdj)

    const nodes = graph.value.nodes.filter((n) => included.has(n.asset_key_str))
    const subEdges = edges.filter((e) => included.has(e.source) && included.has(e.target))
    return { nodes, edges: subEdges }
  }

  /**
   * Fetch the SQLMesh lineage subgraph for a bridged asset and map it into an
   * AssetDagGraph. Asset keys are derived from each node's display ``name`` so
   * they align with the code-location asset keys.
   */
  async function buildSqlmeshSubgraph(key: string): Promise<AssetDagGraph> {
    const modelName = sqlmeshAssetKeyToModelName(key)
    const environment = useEnvironmentStore().currentEnvironment
    const { data } = await api.get<SqlmeshLineageResponse>('/sqlmesh/lineage', {
      params: { workspace_path: currentWorkspacePath, model_name: modelName, environment },
    })
    const lineageGraph = data.graph || {}
    const lineageNodes = data.nodes || {}

    // Resolve an FQN to its Dagster asset key string (via the node's display
    // name; fall back to the FQN when metadata is missing).
    const fqnToKey = (fqn: string): string => {
      const node = lineageNodes[fqn]
      const name = node?.name || fqn
      return sqlmeshModelNameToAssetKey(name)
    }

    const nodes: AssetInfo[] = []
    const edges: { source: string; target: string }[] = []
    for (const [fqn, upstreams] of Object.entries(lineageGraph)) {
      const nodeKey = fqnToKey(fqn)
      const meta = lineageNodes[fqn]
      const deps = (upstreams || []).map(fqnToKey)
      nodes.push({
        asset_key: nodeKey.split('__'),
        asset_key_str: nodeKey,
        // Carry the SQLMesh display name so the canvas shows `schema.table`
        // consistently with /api/v1/sqlmesh/models and /api/v1/sqlmesh/lineage.
        name: meta?.name || sqlmeshAssetKeyToModelName(nodeKey),
        group_name: SQLMESH_PREFIX,
        description: meta ? `SQLMesh model ${meta.name}` : null,
        kind: SQLMESH_PREFIX,
        deps,
      })
      for (const dep of deps) edges.push({ source: dep, target: nodeKey })
    }
    return { nodes, edges }
  }

  /**
   * Select an asset and load its up/down-stream lineage subgraph on demand.
   *
   * - Native Dagster assets: computed locally from the full inventory graph.
   * - SQLMesh assets: fetched from /sqlmesh/lineage (focused subgraph).
   *
   * Passing ``null`` clears the selection and the canvas subgraph.
   */
  async function selectAsset(key: string | null): Promise<void> {
    selectedAssetKey.value = key
    lineageError.value = ''

    if (!key) {
      displayGraph.value = { nodes: [], edges: [] }
      return
    }

    lineageLoading.value = true
    try {
      if (isSqlmeshAssetKey(key)) {
        displayGraph.value = await buildSqlmeshSubgraph(key)
      } else {
        displayGraph.value = buildNativeSubgraph(key)
      }
    } catch (e) {
      lineageError.value = 'Failed to load asset lineage'
      // Keep the previous canvas untouched on failure; surface a toast only.
      notifyError(e, lineageError.value)
    } finally {
      lineageLoading.value = false
    }
  }

  /** Trigger a job run for the workspace. Returns the launched run id. */
  async function triggerRun(
    workspacePath: string,
    jobName: string,
    runConfig?: Record<string, unknown>,
  ): Promise<RunInfo> {
    const { data } = await api.post<RunInfo>('/dagster/pipelines/run', {
      workspace_path: workspacePath,
      job_name: jobName,
      run_config: runConfig,
    })
    runs.value = [data, ...runs.value]
    return data
  }

  /** Refresh the status of a single run (used for polling in-flight runs). */
  async function refreshRun(runId: string): Promise<RunInfo | null> {
    try {
      const { data } = await api.get<RunInfo>(`/dagster/runs/${encodeURIComponent(runId)}`)
      const idx = runs.value.findIndex((r) => r.run_id === runId)
      if (idx >= 0) runs.value[idx] = data
      else runs.value = [data, ...runs.value]
      return data
    } catch (e) {
      console.warn('[dagster] refreshRun failed:', e)
      return null
    }
  }

  /** Fetch structured logs for a run (lazy — expanded rows in the history panel). */
  async function fetchRunLogs(runId: string): Promise<RunLogEvent[]> {
    runsLoading.value = true
    try {
      const { data } = await api.get<RunLogEvent[]>(
        `/dagster/runs/${encodeURIComponent(runId)}/logs`,
      )
      runLogs.value = { ...runLogs.value, [runId]: data }
      return data
    } catch (e) {
      notifyError(e, 'Failed to fetch run logs')
      return []
    } finally {
      runsLoading.value = false
    }
  }

  /** Cancel an in-flight run. */
  async function cancelRun(runId: string): Promise<void> {
    const { data } = await api.post<RunInfo>(`/dagster/runs/${encodeURIComponent(runId)}/cancel`)
    const idx = runs.value.findIndex((r) => r.run_id === runId)
    if (idx >= 0) runs.value[idx] = { ...runs.value[idx], status: data.status }
  }

  /** List schedules for the workspace's code location. */
  async function fetchSchedules(workspacePath: string): Promise<void> {
    if (!workspacePath) return
    schedulesLoading.value = true
    try {
      const { data } = await api.get<ScheduleInfo[]>('/dagster/schedules', {
        params: { workspace_path: workspacePath },
      })
      schedules.value = data || []
    } catch (e) {
      console.warn('[dagster] fetchSchedules failed:', e)
      schedules.value = []
    } finally {
      schedulesLoading.value = false
    }
  }

  /** List active code locations managed by the service (diagnostics). */
  async function fetchCodeLocations(): Promise<CodeLocationInfo[]> {
    const { data } = await api.get<CodeLocationInfo[]>('/dagster/pipelines/code-locations')
    return data || []
  }

  /** Clear all workspace-scoped state (on workspace switch). */
  function reset(): void {
    graph.value = { nodes: [], edges: [] }
    displayGraph.value = { nodes: [], edges: [] }
    selectedAssetKey.value = null
    runs.value = []
    runLogs.value = {}
    schedules.value = []
    errorMsg.value = ''
    lineageError.value = ''
    lineageLoading.value = false
    loadedWorkspacePath = null
    loadedEnvironment = null
    currentWorkspacePath = ''
  }

  return {
    // State
    mode,
    graph,
    displayGraph,
    selectedAssetKey,
    runs,
    runLogs,
    schedules,
    graphLoading,
    lineageLoading,
    lineageError,
    runsLoading,
    schedulesLoading,
    errorMsg,
    // Getters
    selectedAsset,
    upstream,
    downstream,
    isRemote,
    displayNameFor,
    // Actions
    fetchMode,
    loadGraph,
    reloadCodeLocation,
    selectAsset,
    triggerRun,
    refreshRun,
    fetchRunLogs,
    cancelRun,
    fetchSchedules,
    fetchCodeLocations,
    reset,
  }
})
