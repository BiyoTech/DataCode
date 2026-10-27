/**
 * Dagster store — manages Asset Dag state for the active environment.
 *
 * Responsibilities:
 * - Fetch the deployment mode (local/remote) + subprocess health.
 * - Ensure the environment's Dagster deployment (its Code Location) is running,
 *   then load the asset dependency graph for DAG rendering.
 * - List/trigger runs and poll their status; list schedules.
 *
 * A deployment is per-environment: the working copy path identifies both the
 * workspace and the environment, so switching environment switches deployment.
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
  DeploymentDiff,
  DeploymentInfo,
  ModeInfo,
  ReloadJobInfo,
  RunInfo,
  RunLogEvent,
  ScheduleInfo,
  SqlmeshLineageResponse,
} from '@/types/dagster'

// Transcript lines kept in memory for the progress panel. pip is chatty and the
// interesting part is always the end.
const OUTPUT_LINES = 500

// How long the UI waits for a reload before giving up on it. Above the backend's
// own install budget plus the code-location start, so a legitimate first install
// finishes inside it; the job itself is bounded by the sandbox, so this only
// decides how long the PAGE waits.
const RELOAD_WAIT_MS = 20 * 60 * 1000

/**
 * A reload that reached a terminal, non-successful state.
 *
 * Carries the job so a caller can render the structured reason instead of only a
 * message: the failure code decides the wording, and `error_log_id` names the
 * archived record under `{workspace}/logs/errors/`.
 */
export class ReloadFailedError extends Error {
  constructor(readonly job: ReloadJobInfo) {
    super(job.message || 'the reload failed')
    this.name = 'ReloadFailedError'
  }
}

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
  // Deployment backing the loaded graph. Carries the environment, its type and
  // whether its schedules may run, which is what the DAG/schedule views display.
  const deployment = ref<DeploymentInfo | null>(null)
  // The reload in flight (or the last one), and the installer output collected
  // for it. Held in the store rather than in a component so switching views
  // during a ten-minute install does not lose the progress.
  const reloadJob = ref<ReloadJobInfo | null>(null)
  const reloadOutput = ref<string[]>([])

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
   * Ensure the environment's Dagster deployment (its Code Location) is running
   * and refresh ``deployment`` state. Never throws: a failure is logged and the
   * previous deployment info is kept, so callers can always proceed.
   */
  async function ensureDeployment(workspacePath: string): Promise<void> {
    if (!workspacePath) return
    try {
      const { data } = await api.post<DeploymentInfo>('/dagster/deployments/ensure', {
        workspace_path: workspacePath,
      })
      deployment.value = data
    } catch (e) {
      console.warn('[dagster] ensure deployment failed:', e)
    }
  }

  /**
   * Ensure the environment's Dagster deployment is running, then load the full
   * asset inventory (left list + native subgraph source). The canvas stays empty
   * until the user selects an asset.
   *
   * Cached per (workspace path, environment); pass force=true to bypass the
   * cache (e.g. after a deployment reload). Each environment has its own
   * deployment loading its own branch, so switching environments re-fetches.
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
      // The deployment's Code Location must exist before the graph can resolve.
      // Log but continue on failure — the graph query may still return assets
      // from a previously loaded deployment.
      await ensureDeployment(workspacePath)

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

  /**
   * Reload the environment's deployment (after user edits asset-dag/ code).
   *
   * The backend runs this as a JOB: it installs the working copy's declared
   * Python packages before starting the code location, which can take minutes on
   * a first install. So this starts the job and resolves only when it reaches a
   * terminal state, exposing `reloadJob` for the progress panel meanwhile.
   *
   * Progress comes from two sources on purpose. The SSE `deployment` topic is the
   * live one, and polling is the authoritative fallback: a dropped connection, a
   * background tab or a runtime restart must not leave the UI waiting forever on
   * an event that will never arrive.
   *
   * Rejects when the reload failed, so callers keep their existing error handling
   * — but the reason is also left in `reloadJob` for the panel to render, since a
   * toast cannot hold a pip transcript.
   */
  async function reloadDeployment(workspacePath: string): Promise<ReloadJobInfo> {
    if (!workspacePath) throw new Error('workspace path is required')

    const { data } = await api.post<ReloadJobInfo>('/dagster/deployments/reload', {
      workspace_path: workspacePath,
      source: 'ui',
    })
    reloadJob.value = data
    reloadOutput.value = data.output_tail ? data.output_tail.split('\n') : []

    const final = await watchReloadJob(workspacePath, data.job_id)
    if (final.state !== 'succeeded') {
      throw new ReloadFailedError(final)
    }
    if (final.deployment) deployment.value = final.deployment
    await loadGraph(workspacePath, true)
    return final
  }

  /**
   * Apply one reload progress update.
   *
   * Written as a reducer over the job envelope rather than as inline assignments
   * because the same shape arrives from two channels: the status endpoint (what
   * this store polls) and the runtime's SSE `deployment` topic (published for
   * other consumers — the CLI, a second tab — which this app does not subscribe
   * to today). A single reducer is what keeps them from disagreeing about the
   * current stage if that subscription is ever added.
   */
  function applyReloadEvent(event: ReloadJobInfo): void {
    // Ignore anything about a job we are not watching: the topic is per working
    // copy, so a second tab's reload of a different copy can arrive here.
    if (reloadJob.value && event.job_id !== reloadJob.value.job_id) return
    if (event.line) {
      reloadOutput.value = [...reloadOutput.value, event.line].slice(-OUTPUT_LINES)
      // A progress event deliberately carries no tail (it would resend the whole
      // transcript per line), so keep the one accumulated so far.
      reloadJob.value = { ...event, output_tail: reloadJob.value?.output_tail || '' }
      return
    }
    if (event.output_tail) {
      reloadOutput.value = event.output_tail.split('\n').slice(-OUTPUT_LINES)
    }
    reloadJob.value = event
  }

  /**
   * Poll a reload job until it is terminal.
   *
   * The interval is short at the start (a reload with nothing to install finishes
   * in a second) and stretches out, so a ten-minute install does not cost six
   * hundred requests.
   */
  async function watchReloadJob(
    workspacePath: string,
    jobId: string
  ): Promise<ReloadJobInfo> {
    const started = Date.now()
    let delay = 400
    while (Date.now() - started < RELOAD_WAIT_MS) {
      await new Promise((resolve) => setTimeout(resolve, delay))
      delay = Math.min(delay * 1.4, 3000)
      try {
        const { data } = await api.get<ReloadJobInfo>('/dagster/deployments/reload/status', {
          // workspace_path is REQUIRED, not decoration: the runtime is a headless
          // Service and the gateway routes this request to the replica holding
          // this copy's lease. Without it the query reaches a random replica.
          params: { workspace_path: workspacePath, job_id: jobId },
        })
        applyReloadEvent(data)
        if (data.state !== 'running') return data
      } catch (e) {
        // A transient 404/5xx while the job runs must not abort the watch: the
        // job is still going, and the next poll is the cheapest recovery.
        console.warn('[dagster] reload status poll failed:', e)
      }
    }
    throw new Error('reload timed out')
  }

  /** Cancel the reload in flight for a working copy. */
  async function cancelReload(workspacePath: string): Promise<void> {
    const { data } = await api.post<ReloadJobInfo>('/dagster/deployments/reload/cancel', {
      workspace_path: workspacePath,
    })
    applyReloadEvent(data)
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

  /** List the deployments this service serves (diagnostics). */
  async function fetchDeployments(): Promise<DeploymentInfo[]> {
    const { data } = await api.get<DeploymentInfo[]>('/dagster/deployments')
    return data || []
  }

  /**
   * Compare two deployments' asset and schedule definition sets. Both must be
   * loaded — the service reads definitions from the running code locations.
   */
  async function diffDeployments(deploymentName: string, baseName: string): Promise<DeploymentDiff> {
    const { data } = await api.get<DeploymentDiff>('/dagster/deployments/diff', {
      params: { deployment: deploymentName, base: baseName },
    })
    return data
  }

  /** Clear all workspace-scoped state (on workspace/environment switch). */
  function reset(): void {
    graph.value = { nodes: [], edges: [] }
    displayGraph.value = { nodes: [], edges: [] }
    selectedAssetKey.value = null
    runs.value = []
    runLogs.value = {}
    schedules.value = []
    deployment.value = null
    reloadJob.value = null
    reloadOutput.value = []
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
    deployment,
    reloadJob,
    reloadOutput,
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
    ensureDeployment,
    loadGraph,
    reloadDeployment,
    cancelReload,
    selectAsset,
    triggerRun,
    refreshRun,
    fetchRunLogs,
    cancelRun,
    fetchSchedules,
    fetchDeployments,
    diffDeployments,
    reset,
  }
})
