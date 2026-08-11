<script setup lang="ts">
/**
 * OntologyMetricsTab — catalogue of the model's metrics.
 *
 * Each metric is shown as a card tagged with its kind (base / filtered /
 * derived / mapped) and its defining fields (aggregate, expression, filter,
 * derived formula, mapped SQLMesh metric) plus any AI context. A search box
 * filters by name / description / expression.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SemanticModel, Metric } from '@/types/ontology'
import { metricKind, type MetricKind } from '@/utils/ontology'
import AiContextBlock from './AiContextBlock.vue'

const { t } = useI18n()

const props = defineProps<{
  model: SemanticModel
}>()

const query = ref('')

const allMetrics = computed<Metric[]>(() =>
  Object.values(props.model.metrics ?? {}).sort((a, b) => a.name.localeCompare(b.name)),
)

const metrics = computed<Metric[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return allMetrics.value
  return allMetrics.value.filter(
    (m) =>
      m.name.toLowerCase().includes(q) ||
      (m.description ?? '').toLowerCase().includes(q) ||
      (m.expression ?? '').toLowerCase().includes(q),
  )
})

// Badge theme per metric kind — keeps colours consistent with TDesign tags.
const kindTheme: Record<MetricKind, 'primary' | 'success' | 'warning' | 'default'> = {
  base: 'primary',
  filtered: 'success',
  derived: 'warning',
  mapped: 'default',
}

function kindLabel(kind: MetricKind): string {
  return t(`models.ontology.metricKind.${kind}`)
}
</script>

<template>
  <div class="metrics-tab">
    <div v-if="allMetrics.length === 0" class="empty">
      <t-icon name="chart" size="40px" />
      <p>{{ t('models.ontology.noMetrics') }}</p>
    </div>

    <template v-else>
      <div class="toolbar">
        <t-input
          v-model="query"
          :placeholder="t('models.ontology.searchMetrics')"
          clearable
          size="small"
        >
          <template #prefix-icon><t-icon name="search" /></template>
        </t-input>
        <span class="count">{{ metrics.length }} / {{ allMetrics.length }}</span>
      </div>

      <div class="metric-list">
        <div v-for="m in metrics" :key="m.name" class="metric-card">
          <div class="metric-head">
            <span class="metric-name">{{ m.name }}</span>
            <t-tag size="small" variant="light" :theme="kindTheme[metricKind(m)]">
              {{ kindLabel(metricKind(m)) }}
            </t-tag>
            <t-tag v-if="m.aggregate" size="small" variant="outline" class="agg">
              {{ m.aggregate }}
            </t-tag>
          </div>

          <p v-if="m.description" class="metric-desc">{{ m.description }}</p>

          <div class="metric-fields">
            <div v-if="m.expression" class="field">
              <span class="field-label">{{ t('models.ontology.expression') }}</span>
              <code>{{ m.expression }}</code>
            </div>
            <div v-if="m.filter" class="field">
              <span class="field-label">{{ t('models.ontology.filter') }}</span>
              <code>{{ m.filter }}</code>
            </div>
            <div v-if="m.derived" class="field">
              <span class="field-label">{{ t('models.ontology.formula') }}</span>
              <code>{{ m.derived.formula }}</code>
            </div>
            <div v-if="m.maps_to" class="field">
              <span class="field-label">{{ t('models.ontology.mapsTo') }}</span>
              <code>{{ m.maps_to }}</code>
            </div>
            <div v-if="m.entity || m.entities.length" class="field">
              <span class="field-label">{{ t('models.ontology.entities') }}</span>
              <span class="entity-tags">
                <t-tag
                  v-for="e in m.entity ? [m.entity, ...m.entities] : m.entities"
                  :key="e"
                  size="small"
                  variant="light"
                >
                  {{ e }}
                </t-tag>
              </span>
            </div>
          </div>

          <AiContextBlock v-if="m.ai_context" :context="m.ai_context" compact />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.metrics-tab {
  padding: 20px 24px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.toolbar .t-input {
  max-width: 280px;
}

.count {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.metric-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.metric-card {
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.metric-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.metric-name {
  font-weight: 600;
  font-size: var(--td-font-size-body-medium);
  color: var(--td-text-color-primary);
}

.agg {
  font-family: var(--td-font-family-mono);
}

.metric-desc {
  margin: 0;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
  line-height: 1.5;
}

.metric-fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.field-label {
  min-width: 88px;
  font-size: 11px;
  font-weight: 600;
  color: var(--td-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.field code {
  font-family: var(--td-font-family-mono);
  font-size: 12px;
  padding: 2px 8px;
  background: var(--td-bg-color-secondarycontainer);
  border-radius: 4px;
  color: var(--td-text-color-primary);
  word-break: break-all;
}

.entity-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 60px 0;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-medium);
}
</style>
