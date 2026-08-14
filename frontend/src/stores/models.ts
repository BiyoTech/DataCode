/**
 * Models store — manages SQLMesh model tree, detail, lineage, and diff state.
 * API calls go through the gateway at /api/v1/sqlmesh/*.
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { useWorkspaceStore } from './workspace'
import { useEnvironmentStore } from './environment'
import { useGatewayStore } from './gateway'
import api from '@/utils/api'
import { notifyError } from '@/utils/notifyError'
import type {
  ModelSummary,
  ModelDetail,
  ModelGraph,
  ModelTreeNode,
  LineageNodeMap,
  ColumnLineageResult,
} from '@/types/models'

export const useModelsStore = defineStore('models', () => {
  // ─────────── State ───────────
  const models = ref<ModelSummary[]>([])
  const modelsLoading = ref(false)
  const selectedModelName = ref<string | null>(null)
  const modelDetail = ref<ModelDetail | null>(null)
  const detailLoading = ref(false)
  const lineageGraph = ref<ModelGraph>({})
  const lineageNodes = ref<LineageNodeMap>({})
  const lineageLoading = ref(false)
  // Track which model the current lineage data belongs to, so we can decide
  // whether a re-fetch is needed when switching tabs or models.
  const lineageForModel = ref<string | null>(null)
  // History stack of lineage navigations — enables "back to previous lineage".
  const lineageHistory = ref<string[]>([])
  // Model detail shown in the lineage side drawer (independent from the main
  // selection, so opening a node's detail does NOT reload the lineage graph).
  const previewDetail = ref<ModelDetail | null>(null)
  const previewLoading = ref(false)
  const activeTab = ref<'overview' | 'preview' | 'lineage' | 'history' | 'diff'>('overview')

  // ─────────── Computed ───────────
  const modelTree = computed<ModelTreeNode[]>(() => {
    return _buildTreeFromPaths(models.value)
  })

  const selectedModel = computed(() => {
    if (!selectedModelName.value) return null
    return models.value.find(m => m.name === selectedModelName.value) || null
  })

  // ─────────── Actions ───────────

  /** Fetch all models for the current workspace + selected environment + gateway. */
  async function fetchModels() {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) return

    const environment = useEnvironmentStore().currentEnvironment
    // List-level operations honor the globally selected gateway.
    const gateway = useGatewayStore().currentGateway
    modelsLoading.value = true
    try {
      const { data } = await api.get('/sqlmesh/models', {
        params: { workspace_path: ws.path, environment, ...(gateway ? { gateway } : {}) },
      })
      models.value = data.models || []
    } catch (e: any) {
      notifyError(e, 'Failed to fetch models')
      models.value = []
    } finally {
      modelsLoading.value = false
    }
  }

  /**
   * Resolve the gateway a model is bound to from the loaded model list.
   * External models imported from a non-default gateway (e.g. `mysql`) are
   * only present in that gateway's SQLMesh context, so detail / lineage
   * requests MUST carry the gateway or the backend (default context) returns
   * "model not found". Base SQL/Python models have no gateway → undefined.
   */
  function _gatewayFor(modelName: string): string | undefined {
    const m = models.value.find(x => x.name === modelName || x.fqn === modelName)
    return m?.gateway || undefined
  }

  /** Fetch detail for a specific model (columns, SQL, metadata). */
  async function fetchModelDetail(modelName: string) {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) return

    selectedModelName.value = modelName
    detailLoading.value = true
    try {
      const gateway = _gatewayFor(modelName)
      const { data } = await api.get(`/sqlmesh/models/${encodeURIComponent(modelName)}`, {
        params: { workspace_path: ws.path, ...(gateway ? { gateway } : {}) },
      })
      modelDetail.value = data
    } catch (e: any) {
      notifyError(e, 'Failed to fetch model detail')
      modelDetail.value = null
    } finally {
      detailLoading.value = false
    }
  }

  /** Fetch lineage graph for a specific model. */
  async function fetchLineage(modelName?: string) {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) return

    // Record navigation history when switching to a different model
    const target = modelName || selectedModelName.value
    if (target && target !== lineageForModel.value) {
      if (lineageForModel.value) {
        lineageHistory.value.push(lineageForModel.value)
      }
      lineageForModel.value = target
    }

    lineageLoading.value = true
    try {
      // Gateway resolution is layered: a full-graph request (no `modelName`)
      // uses the globally selected gateway, while a single-model lineage
      // request resolves the gateway from the model itself so external models
      // from a non-default gateway still resolve correctly.
      const gateway = modelName
        ? _gatewayFor(modelName)
        : useGatewayStore().currentGateway || undefined
      const environment = useEnvironmentStore().currentEnvironment
      const { data } = await api.get('/sqlmesh/lineage', {
        params: {
          workspace_path: ws.path,
          environment,
          ...(modelName ? { model_name: modelName } : {}),
          ...(gateway ? { gateway } : {}),
        },
      })
      lineageGraph.value = data.graph || {}
      lineageNodes.value = data.nodes || {}
    } catch (e: any) {
      notifyError(e, 'Failed to fetch lineage')
      lineageGraph.value = {}
      lineageNodes.value = {}
    } finally {
      lineageLoading.value = false
    }
  }

  /** Navigate back to the previous lineage view, if any history exists. */
  async function goBackLineage(): Promise<boolean> {
    if (lineageHistory.value.length === 0) return false
    const prev = lineageHistory.value.pop()!
    // Set lineageForModel to prev BEFORE fetch so fetchLineage does not push
    // history again — this implements a true "back" navigation.
    lineageForModel.value = prev
    selectedModelName.value = prev
    await fetchLineage(prev)
    return true
  }

  /**
   * Fetch column-level lineage for a single (model, column) pair.
   * Uses models_only=true → returns only model-level column dependencies
   * (upstream), which is cheap and enough to draw column connection lines.
   * Returns null on failure; caller decides how to surface it.
   */
  async function fetchColumnLineage(
    modelName: string,
    columnName: string,
  ): Promise<ColumnLineageResult | null> {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) return null
    try {
      const gateway = _gatewayFor(modelName)
      const { data } = await api.get(
        `/sqlmesh/lineage/${encodeURIComponent(modelName)}/${encodeURIComponent(columnName)}`,
        { params: { workspace_path: ws.path, models_only: true, ...(gateway ? { gateway } : {}) } },
      )
      return data as ColumnLineageResult
    } catch (e: any) {
      notifyError(e, 'Failed to fetch column lineage')
      return null
    }
  }

  /**
   * Load a model's detail into `previewDetail` for the lineage side drawer.
   * Kept separate from `fetchModelDetail` so it does not mutate the active
   * selection or trigger a lineage reload.
   */
  async function fetchModelPreview(modelName: string) {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) return

    previewLoading.value = true
    try {
      const gateway = _gatewayFor(modelName)
      const { data } = await api.get(`/sqlmesh/models/${encodeURIComponent(modelName)}`, {
        params: { workspace_path: ws.path, ...(gateway ? { gateway } : {}) },
      })
      previewDetail.value = data
    } catch (e: any) {
      notifyError(e, 'Failed to fetch model detail')
      previewDetail.value = null
    } finally {
      previewLoading.value = false
    }
  }

  /** Select a model and load its detail. */
  function selectModel(modelName: string) {
    selectedModelName.value = modelName
    activeTab.value = 'overview'
    fetchModelDetail(modelName)
  }

  function switchTab(tab: typeof activeTab.value) {
    activeTab.value = tab
    // Lazy-load lineage when the tab is opened, OR when the currently loaded
    // lineage data belongs to a different model than the selected one.
    if (tab === 'lineage' && selectedModelName.value) {
      if (
        Object.keys(lineageGraph.value).length === 0 ||
        lineageForModel.value !== selectedModelName.value
      ) {
        fetchLineage(selectedModelName.value)
      }
    }
  }

  function clearSelection() {
    selectedModelName.value = null
    modelDetail.value = null
    activeTab.value = 'overview'
  }

  /** Clear all workspace-scoped model state (used on workspace switch). */
  function reset() {
    models.value = []
    modelsLoading.value = false
    lineageGraph.value = {}
    lineageNodes.value = {}
    lineageForModel.value = null
    lineageHistory.value = []
    lineageLoading.value = false
    previewDetail.value = null
    previewLoading.value = false
    clearSelection()
  }

  // ─────────── Private helpers ───────────

  /**
   * Build a hierarchical tree from model file paths, mirroring the on-disk
   * folder layout under the workspace `models/` directory.
   *
   * Each model exposes `path` (absolute posix path of its source file, e.g.
   * `.../{tenant}/{workspace}/{user}/models/submodel/foo.sql`). We take the
   * portion after the `models/` segment and split it by `/`: intermediate
   * segments become folder nodes, the last segment becomes a model leaf.
   */
  function _buildTreeFromPaths(list: ModelSummary[]): ModelTreeNode[] {
    const root: ModelTreeNode[] = []

    for (const model of list) {
      const parts = _relativeModelPath(model).split('/').filter(Boolean)
      if (parts.length === 0) continue

      let currentChildren = root
      for (let i = 0; i < parts.length; i++) {
        const segment = parts[i]
        const isLeaf = i === parts.length - 1
        const fullName = parts.slice(0, i + 1).join('/')

        // Reuse an existing node of the matching kind, otherwise create it.
        let node = currentChildren.find(
          n => n.name === (isLeaf ? _stripExt(segment) : segment) && n.isDirectory === !isLeaf
        )
        if (!node) {
          node = {
            name: isLeaf ? _stripExt(segment) : segment,
            fullName,
            isDirectory: !isLeaf,
            children: isLeaf ? undefined : [],
            model: isLeaf ? model : undefined,
          }
          currentChildren.push(node)
        }

        if (!isLeaf) {
          currentChildren = node.children!
        }
      }
    }

    _sortTree(root)
    return root
  }

  /**
   * Resolve a model's path relative to the `models/` directory.
   * Falls back to the FQN (dot -> slash) when the path is missing or not
   * located under a `models/` directory, so no model is silently dropped.
   */
  function _relativeModelPath(model: ModelSummary): string {
    const path = model.path || ''
    if (path) {
      const segments = path.split('/')
      const idx = segments.lastIndexOf('models')
      if (idx >= 0 && idx < segments.length - 1) {
        return segments.slice(idx + 1).join('/')
      }
    }
    return model.fqn ? model.fqn.split('.').join('/') : model.name
  }

  /** Strip known model file extensions for display. */
  function _stripExt(name: string): string {
    return name.replace(/\.(sql|py)$/i, '')
  }

  /** Recursively sort tree nodes: folders first, then alphabetical. */
  function _sortTree(nodes: ModelTreeNode[]): void {
    nodes.sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    for (const node of nodes) {
      if (node.children) _sortTree(node.children)
    }
  }

  return {
    // State
    models,
    modelsLoading,
    selectedModelName,
    modelDetail,
    detailLoading,
    lineageGraph,
    lineageNodes,
    lineageLoading,
    lineageForModel,
    lineageHistory,
    previewDetail,
    previewLoading,
    activeTab,
    // Computed
    modelTree,
    selectedModel,
    // Actions
    fetchModels,
    fetchModelDetail,
    fetchLineage,
    goBackLineage,
    fetchColumnLineage,
    fetchModelPreview,
    selectModel,
    switchTab,
    clearSelection,
    reset,
  }
})
