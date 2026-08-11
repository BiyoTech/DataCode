<script setup lang="ts">
/**
 * ModelLineageTab — interactive lineage DAG visualization using @antv/g6.
 *
 * Renders an interactive layered graph (left→right) showing upstream ancestors,
 * the selected model (highlighted), and downstream descendants. Each node is a
 * card with a clickable model-name hyperlink, a type color bar, a column count
 * badge, and a searchable + paginated column list.
 *
 * Features:
 * - Zoom in / out / fit-to-view / lock-drag controls
 * - Model name is a hyperlink that opens the model detail in a side drawer
 *   (without changing the current lineage view)
 * - Column list: per-node fuzzy search + 10-per-page pagination (drawn on the
 *   canvas; search text is captured via a lightweight floating input overlay)
 * - Click one or more column rows to trace column-level (upstream) lineage;
 *   connection lines are drawn between the corresponding fields across models
 * - Three states: loading, empty, error
 */
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import G6, { Graph } from '@antv/g6'
import { useModelsStore } from '@/stores/models'
import {
  registerLineageNode,
  NODE_WIDTH,
  HEADER_HEIGHT,
  SEARCH_HEIGHT,
  getVisibleColumns,
  getColumnRowGeometry,
  computeNodeHeight,
} from './lineageNodeRegister'
import { useLineageLayout } from '@/composables/useLineageLayout'

const { t } = useI18n()
const modelsStore = useModelsStore()

// DOM container ref for the G6 canvas
const canvasRef = ref<HTMLElement>()
let graph: Graph | null = null
// Dedicated group holding the column-level connection lines overlay.
let colEdgeGroup: any = null
let isLocked = ref(false)
const errorMsg = ref('')

// ── Per-node interaction state ──
// Current page index per node id (FQN).
const nodePages = ref<Record<string, number>>({})
// Fuzzy search query per node id (FQN).
const nodeSearch = ref<Record<string, string>>({})
// Selected columns as a set of `${nodeId}::${columnName}` keys.
const selectedColumns = ref<Set<string>>(new Set())
// Cached column lineage results keyed by `${nodeId}::${columnName}`.
const columnLineageByKey = ref<Record<string, any>>({})

// Floating search input overlay anchored over a node's search box.
const searchOverlay = ref<{
  id: string
  label: string
  left: number
  top: number
  width: number
  height: number
  fontSize: number
} | null>(null)
const searchInputRef = ref<HTMLInputElement>()

// Model detail side drawer
const detailDrawerVisible = ref(false)

// Debounced re-render timer (used while typing in the search overlay).
let renderTimer: number | null = null

// Register the custom node type once
registerLineageNode()

/** Strip identifier quotes and lower-case for tolerant FQN matching. */
function normalizeFqn(s: string): string {
  return (s || '').replace(/"/g, '').toLowerCase()
}

/** Split a `${nodeId}::${column}` selection key. */
function splitColumnKey(key: string): { id: string; col: string } {
  const idx = key.indexOf('::')
  return idx < 0 ? { id: key, col: '' } : { id: key.slice(0, idx), col: key.slice(idx + 2) }
}

/**
 * Derive column-level connections and per-node active columns from the cached
 * column lineage results. Connections map upstream (source) fields to the
 * downstream (target) fields that depend on them, restricted to nodes present
 * in the current subgraph.
 */
function computeColumnConnections(): {
  connections: Array<{ sId: string; sCol: string; tId: string; tCol: string }>
  activeColumnsMap: Record<string, Set<string>>
} {
  const connections: Array<{ sId: string; sCol: string; tId: string; tCol: string }> = []
  const activeColumnsMap: Record<string, Set<string>> = {}
  const markActive = (id: string, col: string) => {
    ;(activeColumnsMap[id] ||= new Set()).add(col)
  }

  // Build a normalized-FQN → node-id lookup for the current subgraph.
  const normMap: Record<string, string> = {}
  const ids = new Set<string>()
  for (const [id, deps] of Object.entries(modelsStore.lineageGraph)) {
    ids.add(id)
    deps.forEach((d) => ids.add(d))
  }
  ids.forEach((id) => {
    normMap[normalizeFqn(id)] = id
  })

  // Selected columns are always active (highlighted) even without connections.
  for (const key of selectedColumns.value) {
    const { id, col } = splitColumnKey(key)
    if (id && col) markActive(id, col)
  }

  for (const key of Object.keys(columnLineageByKey.value)) {
    const adjacency = columnLineageByKey.value[key]?.lineage || {}
    for (const modelFqn of Object.keys(adjacency)) {
      const tId = normMap[normalizeFqn(modelFqn)]
      const columns = adjacency[modelFqn] || {}
      for (const colName of Object.keys(columns)) {
        const models = columns[colName]?.models || {}
        for (const upFqn of Object.keys(models)) {
          const sId = normMap[normalizeFqn(upFqn)]
          for (const upCol of models[upFqn] || []) {
            if (tId) markActive(tId, colName)
            if (sId) markActive(sId, upCol)
            if (sId && tId) connections.push({ sId, sCol: upCol, tId, tCol: colName })
          }
        }
      }
    }
  }

  return { connections, activeColumnsMap }
}

/** Initialize or reinitialize the G6 graph instance. */
function initGraph() {
  if (!canvasRef.value) return

  if (graph) {
    graph.destroy()
    graph = null
  }
  colEdgeGroup = null

  const container = canvasRef.value
  const width = container.offsetWidth || 800
  const height = container.offsetHeight || 500

  graph = new G6.Graph({
    container,
    width,
    height,
    // Do NOT auto-fit on every render — fitting is triggered explicitly only
    // on data changes / first load so interaction re-renders (column select,
    // pagination, search) keep the current viewport instead of jumping.
    fitView: false,
    fitViewPadding: 40,
    modes: {
      default: [
        'drag-canvas',
        'zoom-canvas',
        {
          type: 'tooltip',
          formatText: (model: any) => model.label || '',
          offset: 10,
        },
      ],
    },
    defaultNode: {
      type: 'model-lineage-node',
    },
    defaultEdge: {
      type: 'cubic-horizontal',
    },
    plugins: [new G6.Grid()],
  })

  // Node click — dispatch by the shape name that was hit.
  graph.on('node:click', (evt: any) => {
    const shapeName = evt.target?.get('name') || ''
    const model = evt.item?.getModel()
    if (!model) return

    if (shapeName === 'title-link' || shapeName === 'title-underline') {
      openDetail(model.label)
      return
    }
    if (shapeName === 'search-box' || shapeName === 'search-text') {
      openSearch(model)
      return
    }
    if (shapeName === 'pager-prev') {
      changePage(model, -1)
      return
    }
    if (shapeName === 'pager-next') {
      changePage(model, 1)
      return
    }
    if (shapeName.startsWith('colrow::')) {
      toggleColumnSelection(model.id, model.label, shapeName.slice('colrow::'.length))
    }
  })

  // Node hover → highlight state
  graph.on('node:mouseenter', (evt: any) => {
    const item = evt.item
    if (item) graph?.setItemState(item, 'hover', true)
  })
  graph.on('node:mouseleave', (evt: any) => {
    const item = evt.item
    if (item) graph?.setItemState(item, 'hover', false)
  })

  // Close the floating search input when the viewport changes to avoid drift.
  graph.on('wheelzoom', closeSearch)
  graph.on('canvas:dragstart', closeSearch)
  // Redraw column connection lines after a node is dragged.
  graph.on('node:dragend', () => nextTick(() => redrawColumnEdges()))
}

/**
 * Render lineage data into the G6 graph.
 *
 * @param fit When true, re-center the viewport (focus the selected node or
 *   fit-to-view). Used only on data changes / first load. Interaction
 *   re-renders (column select, pagination, search) pass `false` so the
 *   current pan/zoom is preserved and the graph does not jump.
 */
function renderGraph(fit = false) {
  if (!graph) return

  // Capture the current viewport transform (zoom + translate) BEFORE the
  // render. `graph.render()` rebuilds every item and resets the root group
  // matrix to identity, which is what makes the canvas "jump" (snap back to
  // default zoom / position) on interaction re-renders. We restore this exact
  // matrix afterwards unless an explicit re-center was requested via `fit`.
  const prevMatrix = !fit && graph.getGroup ? graph.getGroup().getMatrix() : null

  const selectedFqn = modelsStore.selectedModelName
  const { activeColumnsMap } = computeColumnConnections()
  const { nodes, edges } = useLineageLayout(
    modelsStore.lineageGraph,
    modelsStore.lineageNodes,
    selectedFqn,
    {
      pageMap: nodePages.value,
      searchMap: nodeSearch.value,
      activeColumnsMap,
    },
  )

  if (nodes.length === 0) {
    graph.data({ nodes: [], edges: [] } as any)
    graph.render()
    return
  }

  graph.data({ nodes, edges } as any)
  graph.render()

  // Restore the pre-render viewport immediately so there is no visible jump.
  if (prevMatrix) graph.getGroup().setMatrix(prevMatrix)

  nextTick(() => {
    if (!graph) return
    // Match by id (quoted FQN) or by label (model name), since
    // selectedModelName may hold either form.
    const isNodeSelected = (m: any) => m.id === selectedFqn || m.label === selectedFqn

    graph.getNodes().forEach((node: any) => {
      if (isNodeSelected(node.getModel())) graph!.setItemState(node, 'selected', true)
    })
    // Only re-center on data changes / first load. On interaction re-renders
    // (column select, pagination, search) we keep the user's current viewport
    // untouched — no move, no zoom — restoring the captured matrix again in
    // case any async step nudged it.
    if (fit) {
      const selectedNode = graph.getNodes().find((n: any) => isNodeSelected(n.getModel()))
      if (selectedNode) {
        graph.focusItem(selectedNode, true, { easing: 'easeCubic', duration: 300 })
      } else {
        graph.fitView(40)
      }
    } else if (prevMatrix) {
      graph.getGroup().setMatrix(prevMatrix)
    }

    redrawColumnEdges()
    repositionSearchOverlay()
  })
}

/** Compute a connection endpoint (canvas-logical coords) for a column row. */
function columnEndpoint(model: any, col: string, side: 'left' | 'right') {
  const cols = model.columns || []
  const geo = getColumnRowGeometry(cols, model.page || 0, model.search || '', col)
  const yTop = model.y || 0
  const y = geo.visible
    ? yTop + geo.y
    : yTop + computeNodeHeight(cols, model.page || 0, model.search || '') / 2
  const x = side === 'right' ? (model.x || 0) + NODE_WIDTH : model.x || 0
  return { x, y }
}

/** Redraw the column-level connection lines overlay. */
function redrawColumnEdges() {
  if (!graph) return
  if (!colEdgeGroup || colEdgeGroup.get('destroyed')) {
    colEdgeGroup = (graph.get('group') as any).addGroup({ name: 'col-edge-group' })
  }
  colEdgeGroup.clear()

  const { connections } = computeColumnConnections()
  const nodeMap: Record<string, any> = {}
  graph.getNodes().forEach((n: any) => {
    const m = n.getModel()
    nodeMap[m.id] = m
  })

  for (const c of connections) {
    const s = nodeMap[c.sId]
    const target = nodeMap[c.tId]
    if (!s || !target) continue
    const sp = columnEndpoint(s, c.sCol, 'right')
    const tp = columnEndpoint(target, c.tCol, 'left')
    const dx = Math.max(24, Math.abs(tp.x - sp.x) / 2)
    colEdgeGroup.addShape('path', {
      attrs: {
        path: [
          ['M', sp.x, sp.y],
          ['C', sp.x + dx, sp.y, tp.x - dx, tp.y, tp.x, tp.y],
        ],
        stroke: '#F2994A',
        lineWidth: 2,
        opacity: 0.9,
        endArrow: { path: 'M 4,0 L -4,-4 L -4,4 Z', fill: '#F2994A' },
      },
      name: 'col-edge',
    })
  }

  colEdgeGroup.toFront()
  ;(graph.get('canvas') as any)?.draw?.()
}

/** Open the model detail side drawer for the given model name. */
function openDetail(modelName: string) {
  if (!modelName) return
  detailDrawerVisible.value = true
  modelsStore.fetchModelPreview(modelName)
}

/** Show the floating search input positioned over the node's search box. */
function openSearch(model: any) {
  if (!graph) return
  const zoom = graph.getZoom()
  const p = graph.getCanvasByPoint((model.x || 0) + 10, (model.y || 0) + HEADER_HEIGHT + 5)
  searchOverlay.value = {
    id: model.id,
    label: model.label,
    left: p.x,
    top: p.y,
    width: (NODE_WIDTH - 20) * zoom,
    height: (SEARCH_HEIGHT - 10) * zoom,
    fontSize: Math.max(10, 12 * zoom),
  }
  nextTick(() => searchInputRef.value?.focus())
}

/** Keep the open search overlay aligned with its node after a re-render. */
function repositionSearchOverlay() {
  if (!graph || !searchOverlay.value) return
  const node = graph.getNodes().find((n: any) => n.getModel().id === searchOverlay.value!.id)
  if (!node) {
    searchOverlay.value = null
    return
  }
  const model = node.getModel() as any
  const zoom = graph.getZoom()
  const p = graph.getCanvasByPoint((model.x || 0) + 10, (model.y || 0) + HEADER_HEIGHT + 5)
  searchOverlay.value = {
    ...searchOverlay.value,
    left: p.x,
    top: p.y,
    width: (NODE_WIDTH - 20) * zoom,
    height: (SEARCH_HEIGHT - 10) * zoom,
    fontSize: Math.max(10, 12 * zoom),
  }
}

function closeSearch() {
  searchOverlay.value = null
}

/** Debounced re-render used while typing in the search overlay. */
function scheduleRender() {
  if (renderTimer) window.clearTimeout(renderTimer)
  renderTimer = window.setTimeout(() => renderGraph(), 160)
}

/** Handle typing in the floating search input. */
function onSearchInput(e: Event) {
  const id = searchOverlay.value?.id
  if (!id) return
  const val = (e.target as HTMLInputElement).value
  nodeSearch.value = { ...nodeSearch.value, [id]: val }
  // Reset to first page whenever the query changes.
  nodePages.value = { ...nodePages.value, [id]: 0 }
  scheduleRender()
}

/** Move a node's column list to the previous/next page. */
function changePage(model: any, delta: number) {
  const cols = model.columns || []
  const { totalPages, safePage } = getVisibleColumns(
    cols,
    nodePages.value[model.id] || 0,
    nodeSearch.value[model.id] || '',
  )
  const next = Math.min(Math.max(0, safePage + delta), totalPages - 1)
  nodePages.value = { ...nodePages.value, [model.id]: next }
  renderGraph()
}

/** Toggle a column's selection and (lazily) load its column lineage. */
async function toggleColumnSelection(nodeId: string, nodeLabel: string, col: string) {
  const key = `${nodeId}::${col}`
  const set = new Set(selectedColumns.value)

  if (set.has(key)) {
    set.delete(key)
    const cache = { ...columnLineageByKey.value }
    delete cache[key]
    columnLineageByKey.value = cache
    selectedColumns.value = set
    renderGraph()
    return
  }

  set.add(key)
  selectedColumns.value = set
  const res = await modelsStore.fetchColumnLineage(nodeLabel, col)
  if (res) columnLineageByKey.value = { ...columnLineageByKey.value, [key]: res }
  renderGraph()
}

/** Reset all per-node interaction state (on model / lineage switch). */
function clearColumnState() {
  selectedColumns.value = new Set()
  columnLineageByKey.value = {}
  nodePages.value = {}
  nodeSearch.value = {}
  searchOverlay.value = null
}

// ── Toolbar actions ──
function zoomIn() {
  graph?.zoom(1.2)
}
function zoomOut() {
  graph?.zoom(0.8)
}
function fitView() {
  graph?.fitView(40)
}
function toggleLock() {
  isLocked.value = !isLocked.value
  if (!graph) return
  if (isLocked.value) {
    graph.setMode('default')
    graph.removeBehaviors(['drag-canvas'], 'default')
  } else {
    graph.addBehaviors(['drag-canvas'], 'default')
  }
}

/** Navigate back to the previous lineage view. */
async function goBack() {
  await modelsStore.goBackLineage()
}

// ── Watch for data changes ──
watch(
  () => [modelsStore.lineageGraph, modelsStore.lineageNodes],
  () => {
    if (graph) renderGraph(true)
  },
  { deep: true },
)

// Reset column/search/page state whenever the lineage focus model changes.
watch(
  () => modelsStore.lineageForModel,
  () => {
    clearColumnState()
  },
)

watch(
  () => modelsStore.selectedModelName,
  (newName) => {
    if (newName) modelsStore.fetchLineage(newName)
  },
)

watch(
  () => modelsStore.lineageLoading,
  (loading) => {
    if (loading) return
    // When loading finishes the canvas becomes visible (v-else branch); wait
    // for the DOM update, then (re)initialize and render.
    nextTick(() => {
      if (!graph && canvasRef.value) initGraph()
      if (graph) renderGraph(true)
    })
  },
)

// ── Lifecycle ──
onMounted(() => {
  nextTick(() => {
    initGraph()
    if (
      modelsStore.selectedModelName &&
      (Object.keys(modelsStore.lineageGraph).length === 0 ||
        modelsStore.lineageForModel !== modelsStore.selectedModelName)
    ) {
      modelsStore.fetchLineage(modelsStore.selectedModelName)
    } else {
      renderGraph(true)
    }
  })
})

onBeforeUnmount(() => {
  if (renderTimer) window.clearTimeout(renderTimer)
  if (graph) {
    graph.destroy()
    graph = null
  }
})

// Handle container resize
let resizeObserver: ResizeObserver | null = null
onMounted(() => {
  if (canvasRef.value) {
    resizeObserver = new ResizeObserver(() => {
      if (graph && canvasRef.value) {
        graph.changeSize(canvasRef.value.offsetWidth, canvasRef.value.offsetHeight)
        graph.fitView(40)
        nextTick(() => redrawColumnEdges())
      }
    })
    resizeObserver.observe(canvasRef.value)
  }
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
})
</script>

<template>
  <div class="lineage-tab">
    <!-- Loading state -->
    <div v-if="modelsStore.lineageLoading" class="tab-state">
      <t-icon name="loading" size="24px" class="spinning" />
      <span class="state-text">{{ t('models.lineageLoading') }}</span>
    </div>

    <!-- Error state -->
    <div v-else-if="errorMsg" class="tab-state">
      <t-icon name="error-circle" size="24px" style="color: var(--td-error-color)" />
      <span class="state-text">{{ errorMsg }}</span>
    </div>

    <!-- Empty state -->
    <div v-else-if="Object.keys(modelsStore.lineageGraph).length === 0" class="tab-state">
      <t-icon name="info-circle" size="24px" style="color: var(--td-text-color-placeholder)" />
      <span class="state-text">{{ t('models.lineage.noLineage') }}</span>
    </div>

    <!-- G6 Canvas -->
    <div v-else class="lineage-canvas-wrapper">
      <div ref="canvasRef" class="lineage-canvas" />

      <!-- Hint pill (top-center) -->
      <div class="lineage-hint">
        <t-icon name="help-circle" size="14px" />
        <span>{{ t('models.lineage.columnHint') }}</span>
      </div>

      <!-- Back button (top-left) — visible when lineage history exists -->
      <button
        v-if="modelsStore.lineageHistory.length > 0"
        class="lineage-back-btn"
        :title="t('models.lineage.back')"
        @click="goBack"
      >
        <t-icon name="arrow-left" size="16px" />
        <span class="back-label">{{ t('models.lineage.back') }}</span>
      </button>

      <!-- Floating search input overlay (anchored over a node's search box) -->
      <input
        v-if="searchOverlay"
        ref="searchInputRef"
        class="lineage-search-overlay"
        :style="{
          left: searchOverlay.left + 'px',
          top: searchOverlay.top + 'px',
          width: searchOverlay.width + 'px',
          height: searchOverlay.height + 'px',
          fontSize: searchOverlay.fontSize + 'px',
        }"
        :value="nodeSearch[searchOverlay.id] || ''"
        :placeholder="t('models.lineage.searchFields')"
        @input="onSearchInput"
        @keyup.enter="closeSearch"
        @blur="closeSearch"
      />

      <!-- Floating toolbar (bottom-left) -->
      <div class="lineage-toolbar">
        <button class="toolbar-btn" :title="t('models.lineage.zoomIn')" @click="zoomIn">
          <t-icon name="add" size="16px" />
        </button>
        <button class="toolbar-btn" :title="t('models.lineage.zoomOut')" @click="zoomOut">
          <t-icon name="minus" size="16px" />
        </button>
        <button class="toolbar-btn" :title="t('models.lineage.fitView')" @click="fitView">
          <t-icon name="fullscreen" size="16px" />
        </button>
        <button
          class="toolbar-btn"
          :class="{ active: isLocked }"
          :title="t('models.lineage.lock')"
          @click="toggleLock"
        >
          <t-icon :name="isLocked ? 'lock-on' : 'lock-off'" size="16px" />
        </button>
      </div>
    </div>

    <!-- Model detail side drawer -->
    <t-drawer
      v-model:visible="detailDrawerVisible"
      :header="t('models.lineage.detailTitle')"
      size="420px"
      :footer="false"
    >
      <div v-if="modelsStore.previewLoading" class="drawer-state">
        <t-icon name="loading" size="20px" class="spinning" />
        <span>{{ t('models.lineageLoading') }}</span>
      </div>
      <div v-else-if="modelsStore.previewDetail" class="drawer-detail">
        <div class="drawer-meta">
          <div class="meta-name">{{ modelsStore.previewDetail.name }}</div>
          <t-tag theme="primary" variant="light" size="small">
            {{ modelsStore.previewDetail.type }}
          </t-tag>
        </div>
        <p v-if="modelsStore.previewDetail.description" class="drawer-desc">
          {{ modelsStore.previewDetail.description }}
        </p>

        <div class="drawer-section-title">
          {{ t('models.lineage.columns') }}
          <span class="col-count">{{ modelsStore.previewDetail.columns.length }}</span>
        </div>
        <div class="drawer-columns">
          <div
            v-for="col in modelsStore.previewDetail.columns"
            :key="col.name"
            class="drawer-col-row"
          >
            <span class="col-name">{{ col.name }}</span>
            <span class="col-type">{{ col.type }}</span>
          </div>
          <div v-if="modelsStore.previewDetail.columns.length === 0" class="drawer-empty">
            {{ t('models.lineage.noColumns') }}
          </div>
        </div>
      </div>
      <div v-else class="drawer-state">
        <t-icon name="info-circle" size="20px" style="color: var(--td-text-color-placeholder)" />
        <span>{{ t('models.lineage.noLineage') }}</span>
      </div>
    </t-drawer>
  </div>
</template>

<style scoped>
.lineage-tab {
  position: relative;
  height: 100%;
  min-height: 400px;
}

.tab-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  height: 100%;
  min-height: 400px;
}

.state-text {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.spinning {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.lineage-canvas-wrapper {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 400px;
}

.lineage-canvas {
  width: 100%;
  height: 100%;
  min-height: 400px;
}

/* Hint pill — top-center */
.lineage-hint {
  position: absolute;
  top: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.92);
  color: var(--td-text-color-secondary);
  font-size: var(--td-font-size-body-small);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  backdrop-filter: blur(8px);
  z-index: 10;
  pointer-events: none;
}

/* Back button — top-left */
.lineage-back-btn {
  position: absolute;
  top: 16px;
  left: 16px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  background: rgba(255, 255, 255, 0.92);
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-small);
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  backdrop-filter: blur(8px);
  transition: all 0.15s ease;
  z-index: 11;
}
.lineage-back-btn:hover {
  background: var(--td-bg-color-container-hover);
  color: var(--td-brand-color);
  border-color: var(--td-brand-color);
}

/* Floating search input positioned over a node's canvas search box */
.lineage-search-overlay {
  position: absolute;
  padding: 0 8px;
  border: 1px solid var(--td-brand-color);
  border-radius: 6px;
  background: #ffffff;
  color: var(--td-text-color-primary);
  outline: none;
  box-shadow: 0 2px 8px rgba(54, 110, 244, 0.25);
  z-index: 12;
}

/* Floating toolbar — bottom-left */
.lineage-toolbar {
  position: absolute;
  bottom: 16px;
  left: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 6px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  backdrop-filter: blur(8px);
  z-index: 10;
}

.toolbar-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: var(--td-radius-small);
  background: transparent;
  color: var(--td-text-color-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
}

.toolbar-btn:hover {
  background: var(--td-bg-color-container-hover);
  color: var(--td-brand-color);
}

.toolbar-btn.active {
  background: var(--td-brand-color-light);
  color: var(--td-brand-color);
}

/* ── Detail drawer ── */
.drawer-state {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px 0;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-small);
}

.drawer-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}
.meta-name {
  font-size: var(--td-font-size-title-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  word-break: break-all;
}
.drawer-desc {
  margin: 10px 0 0;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
  line-height: 1.6;
}

.drawer-section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 20px 0 8px;
  font-size: var(--td-font-size-body-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
}
.col-count {
  padding: 0 8px;
  border-radius: 10px;
  background: var(--td-bg-color-component);
  color: var(--td-text-color-secondary);
  font-size: var(--td-font-size-body-small);
  font-weight: 500;
}

.drawer-columns {
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  overflow: hidden;
}
.drawer-col-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: var(--td-font-size-body-small);
  border-bottom: 1px solid var(--td-component-stroke);
}
.drawer-col-row:last-child {
  border-bottom: none;
}
.col-name {
  color: var(--td-text-color-primary);
  word-break: break-all;
}
.col-type {
  color: var(--td-text-color-placeholder);
  white-space: nowrap;
}
.drawer-empty {
  padding: 16px;
  text-align: center;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-small);
}
</style>
