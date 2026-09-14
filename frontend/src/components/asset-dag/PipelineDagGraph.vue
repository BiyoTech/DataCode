<script setup lang="ts">
/**
 * PipelineDagGraph — interactive Asset Dag visualization using @antv/g6.
 *
 * Renders the workspace's Data+AI asset dependency graph as a layered DAG
 * (left -> right). Each node is a compact card (see assetDagNodeRegister). Node
 * selection is driven through the shared Dagster store so sibling panels (asset
 * list, detail) stay in sync.
 *
 * Interaction: drag-canvas / zoom-canvas / tooltip / zoom-in / zoom-out /
 * fit-view / lock-drag / reload-code-location. States: loading, error, empty.
 *
 * The dark-tech aesthetic (deep slate canvas + neon accents) is applied here
 * rather than relying on TDesign light tokens used by the lineage tab.
 */
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import G6, { Graph } from '@antv/g6'
import { useDagsterStore } from '@/stores/dagster'
import { useThemeStore } from '@/stores/theme'
import { registerAssetDagNode } from './assetDagNodeRegister'
import { useAssetDagLayout } from '@/composables/useAssetDagLayout'
import { notifyError } from '@/utils/notifyError'
import DeploymentReloadProgress from './DeploymentReloadProgress.vue'

const props = defineProps<{ workspacePath: string }>()

const { t } = useI18n()
const dagster = useDagsterStore()
const theme = useThemeStore()

// DOM container ref for the G6 canvas.
const canvasRef = ref<HTMLElement>()
let graph: Graph | null = null
// Observes the canvas for size changes; recreated whenever the canvas remounts.
let resizeObserver: ResizeObserver | null = null
const isLocked = ref(false)
const reloading = ref(false)

// Register the custom node type once (idempotent).
registerAssetDagNode()

/** Initialize (or reinitialize) the G6 graph instance. */
function initGraph() {
  if (!canvasRef.value) return

  if (graph) {
    graph.destroy()
    graph = null
  }

  // Drop the resize observer bound to the previous (now detached) canvas so
  // observeResize() re-attaches to the freshly mounted container. Without this,
  // a workspace switch would leave the observer watching a stale node.
  resizeObserver?.disconnect()
  resizeObserver = null

  const container = canvasRef.value
  const width = container.offsetWidth || 800
  const height = container.offsetHeight || 500

  graph = new G6.Graph({
    container,
    width,
    height,
    // Explicit fit only on data changes (see renderGraph) to avoid viewport
    // jumps on selection re-renders.
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
    defaultNode: { type: 'asset-dag-node' },
    defaultEdge: { type: 'cubic-horizontal' },
  })

  // Node click -> select the asset (drives detail panel + highlight).
  graph.on('node:click', (evt: any) => {
    const model = evt.item?.getModel()
    if (model?.id) dagster.selectAsset(model.id)
  })

  // Hover highlight.
  graph.on('node:mouseenter', (evt: any) => {
    if (evt.item) graph?.setItemState(evt.item, 'hover', true)
  })
  graph.on('node:mouseleave', (evt: any) => {
    if (evt.item) graph?.setItemState(evt.item, 'hover', false)
  })
}

/**
 * Render the current asset graph into G6.
 *
 * @param fit When true, re-center the viewport (fit-to-view). Used on data
 *   changes / first load only; selection re-renders preserve the viewport.
 */
function renderGraph(fit = false) {
  if (!graph) return

  // Sync G6's canvas size to the container's *current* dimensions before
  // laying out. On first paint the flex container (.adv-center { flex: 1 })
  // may still measure 0/stale while the browser is mid-reflow, so G6 would
  // otherwise fit the DAG against a wrong viewport and push every node off
  // screen — the exact "blank canvas until you switch tabs" symptom. Doing
  // this every render keeps the graph correct across resizes too.
  if (canvasRef.value) {
    const w = canvasRef.value.offsetWidth
    const h = canvasRef.value.offsetHeight
    if (w > 0 && h > 0) graph.changeSize(w, h)
  }

  const prevMatrix = !fit && graph.getGroup ? graph.getGroup().getMatrix() : null

  // Render the selected asset's focused lineage subgraph (not the full graph).
  const { nodes, edges } = useAssetDagLayout(dagster.displayGraph, dagster.selectedAssetKey)

  if (nodes.length === 0) {
    graph.data({ nodes: [], edges: [] } as any)
    graph.render()
    return
  }

  graph.data({ nodes, edges } as any)
  graph.render()

  if (prevMatrix) graph.getGroup().setMatrix(prevMatrix)

  nextTick(() => {
    if (!graph) return
    // Re-apply the selected state after render rebuilds items.
    graph.getNodes().forEach((node: any) => {
      if (node.getModel().id === dagster.selectedAssetKey) {
        graph!.setItemState(node, 'selected', true)
      }
    })
    if (fit) {
      const selected = graph.getNodes().find((n: any) => n.getModel().id === dagster.selectedAssetKey)
      if (selected) {
        graph.focusItem(selected, true, { easing: 'easeCubic', duration: 300 })
      } else {
        graph.fitView(40)
      }
    } else if (prevMatrix) {
      graph.getGroup().setMatrix(prevMatrix)
    }
  })
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

/**
 * Reload this environment's deployment (picks up user edits under asset-dag/).
 *
 * The failure is REPORTED, not swallowed. It used to be discarded, which meant a
 * dependency that could not be installed or code that could not be imported
 * looked exactly like a graph that simply had not changed.
 */
async function reload() {
  if (!props.workspacePath || reloading.value) return
  reloading.value = true
  try {
    await dagster.reloadDeployment(props.workspacePath)
  } catch (e) {
    notifyError(e, t('deploy.reloadFailed'))
  } finally {
    reloading.value = false
  }
}

/**
 * The declaration was rewritten from the failure panel, so the reload that
 * failed on it can now be retried. Retried rather than only refreshed: the user
 * asked for a reload, the platform told them exactly what stood in the way, and
 * they resolved it — making them press the button a second time adds nothing.
 */
async function retryAfterDepsFix() {
  await reload()
}

/**
 * Ensure the G6 instance exists, then render.
 *
 * On first load the graph data arrives asynchronously: the initial mount shows
 * the "empty"/loading state (no canvas in the DOM), so `initGraph()` would run
 * against a missing container. Once data arrives the canvas mounts, but nothing
 * re-initializes G6 — leaving a blank canvas. This helper lazily initializes G6
 * after the canvas DOM is present.
 *
 * We wait through nextTick (Vue DOM patch) AND a rAF (browser layout/reflow)
 * so the flex container has non-zero dimensions before G6 measures it —
 * otherwise the DAG is laid out against a 0-size viewport and appears blank
 * until a tab switch forces a re-measure.
 */
function ensureRenderedGraph(fit: boolean) {
  nextTick(() => {
    requestAnimationFrame(() => {
      // (Re)initialize G6 whenever it is missing OR bound to a stale container.
      // On workspace switch, store.reset() empties the graph and toggles the
      // loading flag, which unmounts the canvas (v-else) and mounts a BRAND NEW
      // <div> once data arrives. The previous G6 instance survives but still
      // points at the detached old DOM node, so rendering into it draws nothing
      // — the "blank until you switch tabs" symptom. Comparing the instance's
      // container against the current canvasRef detects that swap and forces a
      // rebuild against the live node.
      if (!graph || !isGraphBoundToCurrentCanvas()) initGraph()
      if (graph) renderGraph(fit)
      // The canvas only mounts alongside data, so (re)attach the resize observer
      // here rather than at mount, when the container may not yet exist.
      observeResize()
    })
  })
}

/**
 * Whether the live G6 instance is still attached to the current canvas element.
 * Returns false when the canvas was remounted (e.g. workspace switch toggled
 * the loading/empty state), signaling that G6 must be rebuilt.
 */
function isGraphBoundToCurrentCanvas(): boolean {
  if (!graph || !canvasRef.value) return false
  const container = graph.getContainer?.()
  // G6 renders a <canvas>/<svg> child into the provided container div; treat a
  // mismatch (or a container detached from the document) as stale.
  return !!container && canvasRef.value.contains(container as Node)
}

/** Attach the resize observer to the canvas once it exists (idempotent). */
function observeResize() {
  if (!canvasRef.value || resizeObserver) return
  resizeObserver = new ResizeObserver(() => {
    if (graph && canvasRef.value) {
      graph.changeSize(canvasRef.value.offsetWidth, canvasRef.value.offsetHeight)
      graph.fitView(40)
    }
  })
  resizeObserver.observe(canvasRef.value)
}

// ── Watchers ──
// Re-render (and re-fit) whenever the displayed subgraph changes OR the lineage
// loading flag flips to false. selectAsset() updates displayGraph after an async
// fetch (SQLMesh) or a local computation (native), so watching the subgraph plus
// the loading flag guarantees we (re)initialize G6 once the canvas is actually
// mounted with data — mirroring the original graph/loading dance.
watch(
  [() => dagster.displayGraph, () => dagster.lineageLoading],
  () => {
    if (dagster.lineageLoading) return
    ensureRenderedGraph(true)
  },
  { deep: true },
)

// Canvas nodes read themed colors at draw time, so re-render when the app theme
// switches to keep the DAG consistent with the light/dark shell.
watch(
  () => theme.resolved,
  () => {
    if (graph) renderGraph(false)
  },
)

// ── Lifecycle ──
// If graph data is already present at mount, initialize + render immediately;
// otherwise the graph watcher handles the first render once data arrives.
onMounted(() => {
  ensureRenderedGraph(true)
})

onBeforeUnmount(() => {
  if (graph) {
    graph.destroy()
    graph = null
  }
})

// Handle container resize. The observer is attached in ensureRenderedGraph once
// the canvas is in the DOM (it is absent while the empty state is shown).
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
})
</script>

<template>
  <div class="dag-graph">
    <!-- Full-inventory loading state (initial asset list fetch) -->
    <div v-if="dagster.graphLoading" class="dag-state">
      <t-icon name="loading" size="24px" class="spinning" />
      <span class="state-text">{{ t('assetDag.graphLoading') }}</span>
    </div>

    <!-- Inventory load error -->
    <div v-else-if="dagster.errorMsg" class="dag-state">
      <t-icon name="error-circle" size="24px" class="state-error" />
      <span class="state-text">{{ dagster.errorMsg }}</span>
    </div>

    <!-- No asset selected yet: prompt the user to pick one from the list -->
    <div v-else-if="!dagster.selectedAssetKey" class="dag-state">
      <t-icon name="cursor" size="24px" class="state-muted" />
      <span class="state-text">{{ t('assetDag.selectAssetToViewLineage') }}</span>
    </div>

    <!-- Selected asset lineage is loading -->
    <div v-else-if="dagster.lineageLoading" class="dag-state">
      <t-icon name="loading" size="24px" class="spinning" />
      <span class="state-text">{{ t('assetDag.lineageLoading') }}</span>
    </div>

    <!-- Lineage load error for the selected asset -->
    <div v-else-if="dagster.lineageError" class="dag-state">
      <t-icon name="error-circle" size="24px" class="state-error" />
      <span class="state-text">{{ dagster.lineageError }}</span>
    </div>

    <!-- Selected asset has no lineage nodes to render -->
    <div v-else-if="dagster.displayGraph.nodes.length === 0" class="dag-state">
      <t-icon name="chart-bubble" size="24px" class="state-muted" />
      <span class="state-text">{{ t('assetDag.noLineage') }}</span>
    </div>

    <!-- G6 canvas -->
    <div v-else class="dag-canvas-wrapper">
      <div ref="canvasRef" class="dag-canvas" />

      <!-- Floating toolbar (bottom-left) -->
      <div class="dag-toolbar">
        <button class="toolbar-btn" :title="t('assetDag.zoomIn')" @click="zoomIn">
          <t-icon name="add" size="16px" />
        </button>
        <button class="toolbar-btn" :title="t('assetDag.zoomOut')" @click="zoomOut">
          <t-icon name="minus" size="16px" />
        </button>
        <button class="toolbar-btn" :title="t('assetDag.fitView')" @click="fitView">
          <t-icon name="fullscreen" size="16px" />
        </button>
        <button
          class="toolbar-btn"
          :class="{ active: isLocked }"
          :title="t('assetDag.lock')"
          @click="toggleLock"
        >
          <t-icon :name="isLocked ? 'lock-on' : 'lock-off'" size="16px" />
        </button>
        <button class="toolbar-btn" :title="t('assetDag.reload')" :disabled="reloading" @click="reload">
          <t-icon name="refresh" size="16px" :class="{ spinning: reloading }" />
        </button>
      </div>

      <!-- Reload progress, floating over the canvas: an install can run for
           minutes, and the user has to see WHICH stage it is in and why it
           stopped without leaving the graph. -->
      <div v-if="dagster.reloadJob" class="dag-reload-progress">
        <DeploymentReloadProgress
          :job="dagster.reloadJob"
          :output="dagster.reloadOutput"
          :workspace-path="props.workspacePath"
          @deps-fixed="retryAfterDepsFix"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.dag-graph {
  position: relative;
  height: 100%;
  min-height: 400px;
  /* Canvas backdrop follows the app theme so it blends with the main shell. */
  background: var(--bg-primary);
}

/* Top-right, wide enough for the stage ladder and the output panel, and above
   the canvas so a running install is visible without scrolling. */
.dag-reload-progress {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 10;
  width: 460px;
  max-width: calc(100% - 24px);
}

.dag-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  height: 100%;
  min-height: 400px;
}

.state-text {
  font-size: 13px;
  color: var(--text-secondary);
}
.state-error {
  color: var(--error);
}
.state-muted {
  color: var(--text-muted);
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

.dag-canvas-wrapper {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 400px;
}

.dag-canvas {
  width: 100%;
  height: 100%;
  min-height: 400px;
}

/* Floating toolbar — bottom-left */
.dag-toolbar {
  position: absolute;
  bottom: 16px;
  left: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 6px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow-float-lg);
  z-index: 10;
}

.toolbar-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--transition-base);
}

.toolbar-btn:hover {
  background: var(--bg-hover);
  color: var(--accent);
}

.toolbar-btn.active {
  background: var(--accent-subtle);
  color: var(--accent);
}

.toolbar-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
