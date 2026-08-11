<script setup lang="ts">
/**
 * OntologyGraphTab — interactive entity-relationship graph.
 *
 * Renders the semantic model as a classic force-directed dot graph (ECharts):
 *   - nodes = entities, drawn as coloured circles labelled with the entity
 *             name; size and colour scale with the entity's connection degree
 *   - edges = relationships, drawn as curved lines (no inline label)
 *
 * Selection drives a side detail panel instead of cluttering the canvas:
 *   - clicking a node  -> shows its primary key and synonyms
 *   - clicking an edge -> shows the relationship name and its verbalizations
 *
 * The chart stays interactive (drag, zoom, pan, hover tooltip) and is mounted
 * lazily by the parent (`v-if`) so ECharts is only initialised on first open.
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ECharts, EChartsOption } from 'echarts'
import type { Entity, Relationship, SemanticModel } from '@/types/ontology'
import { normaliseAiContext } from '@/utils/ontology'

const { t } = useI18n()

const props = defineProps<{
  model: SemanticModel
}>()

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const entities = computed(() => Object.values(props.model.entities ?? {}))
const relationships = computed(() => Object.values(props.model.relationships ?? {}))
const isEmpty = computed(() => entities.value.length === 0)

// Current selection, rendered in the side panel. Kept in shallowRef since the
// referenced domain objects are immutable snapshots from the model.
const selectedEntity = shallowRef<Entity | null>(null)
const selectedRelationship = shallowRef<Relationship | null>(null)

// Categorical palette used to colour nodes (echoes the reference design).
const PALETTE = [
  '#5b8ff9', '#61ddaa', '#65789b', '#f6bd16', '#7262fd',
  '#78d3f8', '#9661bc', '#f6903d', '#008685', '#f08bb4',
]

/** Pull the synonyms (if any) out of an entity's AI context. */
function entitySynonyms(e: Entity): string[] {
  return normaliseAiContext(e.ai_context)?.synonyms ?? []
}

/** Human-readable primary-key string(s), e.g. "customer_id (id)". */
function entityKeys(e: Entity): string[] {
  return Object.entries(e.key ?? {}).map(([attr, col]) =>
    col && col !== attr ? `${attr} (${col})` : attr,
  )
}

/** Count how many relationships touch each entity (its connection degree). */
function degreeMap(): Record<string, number> {
  const deg: Record<string, number> = {}
  for (const r of relationships.value) {
    if (r.from_entity) deg[r.from_entity] = (deg[r.from_entity] ?? 0) + 1
    if (r.to_entity) deg[r.to_entity] = (deg[r.to_entity] ?? 0) + 1
  }
  return deg
}

/** Build the ECharts option from the current semantic model. */
function buildOption(): EChartsOption {
  const deg = degreeMap()
  const maxDeg = Math.max(1, ...Object.values(deg))

  const nodes = entities.value.map((e, i) => {
    const d = deg[e.name] ?? 0
    return {
      id: e.name,
      name: e.name,
      // Size grows with connection degree for visual hierarchy.
      symbolSize: 22 + (d / maxDeg) * 34,
      value: d,
      itemStyle: { color: PALETTE[i % PALETTE.length] },
      _key: entityKeys(e),
      _synonyms: entitySynonyms(e),
    }
  })

  const links = relationships.value
    // Skip edges whose endpoints are missing from the node set.
    .filter((r) => r.from_entity && r.to_entity)
    .map((r) => ({
      source: r.from_entity,
      target: r.to_entity,
      name: r.name,
    }))

  return {
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: (p: any) => {
        if (p.dataType === 'edge') {
          return `<b>${p.data.name}</b><br/>${p.data.source} → ${p.data.target}`
        }
        return `<b>${p.data.name}</b><br/>${t('models.ontology.graph.connections')}: ${p.data.value}`
      },
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: {
          repulsion: 320,
          edgeLength: [90, 180],
          gravity: 0.08,
          friction: 0.15,
        },
        scaleLimit: { min: 0.4, max: 2.5 },
        label: {
          show: true,
          position: 'right',
          color: '#4e5969',
          fontSize: 12,
          formatter: '{b}',
        },
        // Hide overlapping labels automatically (per the ECharts demo).
        labelLayout: { hideOverlap: true },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: [0, 8],
        lineStyle: {
          color: 'source',
          width: 1.4,
          curveness: 0.15,
          opacity: 0.55,
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: { width: 3, opacity: 0.9 },
          label: { fontWeight: 'bold' },
        },
        // Cast to any: nodes/links carry extra fields (_key, _synonyms)
        // consumed by the tooltip formatter, which the strict item types reject.
        data: nodes as any,
        links: links as any,
      },
    ],
  }
}

function render() {
  if (!chart) return
  chart.setOption(buildOption(), true)
}

/** Wire click handlers so selection drives the detail panel. */
function bindEvents() {
  if (!chart) return
  chart.on('click', (params: any) => {
    if (params.dataType === 'edge') {
      selectedEntity.value = null
      selectedRelationship.value =
        relationships.value.find((r) => r.name === params.data.name) ?? null
    } else if (params.dataType === 'node') {
      selectedRelationship.value = null
      selectedEntity.value = entities.value.find((e) => e.name === params.data.name) ?? null
    }
  })
  // Click empty canvas to clear the selection.
  chart.getZr().on('click', (e: any) => {
    if (!e.target) clearSelection()
  })
}

function clearSelection() {
  selectedEntity.value = null
  selectedRelationship.value = null
}

async function initChart() {
  if (!chartEl.value || chart) return
  const echarts = await import('echarts')
  chart = echarts.init(chartEl.value)
  render()
  bindEvents()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartEl.value)
}

/** Reset zoom/pan, re-run the layout and clear any selection. */
function resetView() {
  clearSelection()
  render()
}

// ─────────── Detail-panel view models ───────────
const detailSynonyms = computed(() => (selectedEntity.value ? entitySynonyms(selectedEntity.value) : []))
const detailKeys = computed(() => (selectedEntity.value ? entityKeys(selectedEntity.value) : []))

onMounted(async () => {
  await nextTick()
  if (!isEmpty.value) await initChart()
})

// Re-render when the model changes (e.g. switching ontologies while on this tab).
watch(
  () => props.model,
  async () => {
    clearSelection()
    await nextTick()
    if (isEmpty.value) return
    if (!chart) await initChart()
    else render()
  },
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="graph-tab">
    <div v-if="isEmpty" class="empty">
      <t-icon name="relativity" size="40px" />
      <p>{{ t('models.ontology.noEntities') }}</p>
    </div>

    <template v-else>
      <!-- Floating legend, matching the reference design. -->
      <div class="graph-legend">
        <div class="legend-title">{{ t('models.ontology.graph.legendTitle') }}</div>
        <div class="legend-row">
          <span class="legend-entity"></span>
          <span>{{ t('models.ontology.graph.legendEntity') }}</span>
        </div>
        <div class="legend-row">
          <span class="legend-relation"></span>
          <span>{{ t('models.ontology.graph.legendRelation') }}</span>
        </div>
      </div>

      <t-button
        class="graph-reset"
        theme="default"
        variant="outline"
        size="small"
        @click="resetView"
      >
        <template #icon><t-icon name="refresh" /></template>
        {{ t('models.ontology.graph.reset') }}
      </t-button>

      <div ref="chartEl" class="graph-canvas"></div>

      <!-- Detail panel: driven by node / edge selection. -->
      <div class="graph-detail">
        <!-- Entity detail -->
        <template v-if="selectedEntity">
          <div class="detail-head">
            <t-tag theme="primary" variant="light" size="small">
              {{ t('models.ontology.graph.entityDetail') }}
            </t-tag>
            <t-button theme="default" variant="text" size="small" shape="square" @click="clearSelection">
              <t-icon name="close" />
            </t-button>
          </div>
          <h4 class="detail-title">{{ selectedEntity.name }}</h4>
          <p v-if="selectedEntity.model" class="detail-model">{{ selectedEntity.model }}</p>

          <div class="detail-section">
            <div class="detail-label">{{ t('models.ontology.key') }}</div>
            <div v-if="detailKeys.length" class="detail-chips">
              <code v-for="k in detailKeys" :key="k" class="chip-code">{{ k }}</code>
            </div>
            <span v-else class="detail-muted">—</span>
          </div>

          <div class="detail-section">
            <div class="detail-label">{{ t('models.ontology.synonyms') }}</div>
            <div v-if="detailSynonyms.length" class="detail-chips">
              <t-tag v-for="s in detailSynonyms" :key="s" size="small" variant="light">{{ s }}</t-tag>
            </div>
            <span v-else class="detail-muted">{{ t('models.ontology.graph.noSynonyms') }}</span>
          </div>
        </template>

        <!-- Relationship detail -->
        <template v-else-if="selectedRelationship">
          <div class="detail-head">
            <t-tag theme="success" variant="light" size="small">
              {{ t('models.ontology.graph.relationDetail') }}
            </t-tag>
            <t-button theme="default" variant="text" size="small" shape="square" @click="clearSelection">
              <t-icon name="close" />
            </t-button>
          </div>
          <h4 class="detail-title">{{ selectedRelationship.name }}</h4>
          <p class="detail-model">
            {{ selectedRelationship.from_entity }} → {{ selectedRelationship.to_entity }}
          </p>

          <div class="detail-section">
            <div class="detail-label">{{ t('models.ontology.graph.verbalizes') }}</div>
            <ul v-if="selectedRelationship.verbalizes && selectedRelationship.verbalizes.length" class="detail-list">
              <li v-for="(v, i) in selectedRelationship.verbalizes" :key="i">{{ v }}</li>
            </ul>
            <span v-else class="detail-muted">—</span>
          </div>
        </template>

        <!-- Empty selection hint -->
        <div v-else class="detail-hint">
          <t-icon name="cursor" size="20px" />
          <span>{{ t('models.ontology.graph.selectHint') }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.graph-tab {
  position: relative;
  height: 100%;
  min-height: 460px;
  display: flex;
  border-radius: 8px;
  overflow: hidden;
  background: linear-gradient(160deg, #f7f9fc 0%, #eef2f9 100%);
}

.graph-canvas {
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 460px;
}

/* ─────────── Floating legend ─────────── */
.graph-legend {
  position: absolute;
  top: 16px;
  left: 16px;
  z-index: 2;
  padding: 12px 14px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 6px 20px rgba(23, 43, 77, 0.08);
  backdrop-filter: blur(4px);
}

.legend-title {
  font-size: 13px;
  font-weight: 600;
  color: #1f2329;
  margin-bottom: 8px;
}

.legend-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #4e5969;
  line-height: 22px;
}

.legend-entity {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 3px solid var(--td-brand-color, #5b8ff9);
  box-sizing: border-box;
}

.legend-relation {
  position: relative;
  width: 22px;
  height: 2px;
  background: #16b364;
}

.legend-relation::after {
  content: '';
  position: absolute;
  right: -2px;
  top: 50%;
  transform: translateY(-50%);
  border-left: 6px solid #16b364;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
}

/* ─────────── Reset button ─────────── */
.graph-reset {
  position: absolute;
  top: 16px;
  right: 16px;
  z-index: 2;
}

/* ─────────── Detail panel ─────────── */
.graph-detail {
  position: absolute;
  right: 16px;
  bottom: 16px;
  z-index: 2;
  width: 260px;
  max-height: calc(100% - 80px);
  overflow-y: auto;
  padding: 14px 16px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 6px 20px rgba(23, 43, 77, 0.1);
  backdrop-filter: blur(4px);
}

.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.detail-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
  word-break: break-all;
}

.detail-model {
  margin: 2px 0 12px;
  font-size: 12px;
  color: #8a9099;
  word-break: break-all;
}

.detail-section {
  margin-bottom: 12px;
}

.detail-label {
  font-size: 11px;
  color: #b1b7c0;
  margin-bottom: 6px;
}

.detail-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip-code {
  padding: 2px 8px;
  border-radius: 4px;
  background: #f2f4f7;
  color: #4e5969;
  font-size: 12px;
  font-family: var(--td-font-family-mono, monospace);
}

.detail-list {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: #4e5969;
  line-height: 20px;
}

.detail-muted {
  font-size: 12px;
  color: #b1b7c0;
}

.detail-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #8a9099;
}

.empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-medium);
}
</style>
