<script setup lang="ts">
/**
 * OntologyDetail — detail panel for the selected ontology (semantic model).
 *
 * Layout: a header (name / path / version / validation status) plus three tabs:
 *   1. Overview — version, description, AI context, entity list.
 *   2. Graph    — interactive entity-relationship visualisation.
 *   3. Metrics  — the metric catalogue.
 *
 * The full semantic model is fetched once here and passed down to each tab, so
 * switching tabs never triggers a refetch.
 */
import { ref, watch, onMounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { useOntologyStore } from '@/stores/ontology'
import type { SemanticModel, OntologyValidationError } from '@/types/ontology'
import OntologyOverviewTab from './OntologyOverviewTab.vue'
import OntologyGraphTab from './OntologyGraphTab.vue'
import OntologyMetricsTab from './OntologyMetricsTab.vue'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const ontologyStore = useOntologyStore()

const props = defineProps<{
  name: string
}>()

type TabKey = 'overview' | 'graph' | 'metrics'
const activeTab = ref<TabKey>('overview')

const model = ref<SemanticModel | null>(null)
const validationErrors = ref<OntologyValidationError[]>([])
const isLoading = ref(false)
const loadError = ref<'notFound' | 'unknown' | null>(null)
const relativePath = ref('')

const tabs = computed<{ key: TabKey; label: string; icon: string }[]>(() => [
  { key: 'overview', label: t('models.ontology.tabs.overview'), icon: 'info-circle' },
  { key: 'graph', label: t('models.ontology.tabs.graph'), icon: 'relativity' },
  { key: 'metrics', label: t('models.ontology.tabs.metrics'), icon: 'chart' },
])

// Counters shown as small badges on the tab labels.
const entityCount = computed(() => Object.keys(model.value?.entities ?? {}).length)
const metricCount = computed(() => Object.keys(model.value?.metrics ?? {}).length)

const errorCount = computed(
  () => validationErrors.value.filter((e) => e.severity === 'error').length,
)
const warningCount = computed(
  () => validationErrors.value.filter((e) => e.severity === 'warning').length,
)

async function load(name: string) {
  const ws = workspaceStore.currentWorkspace
  if (!ws) return
  isLoading.value = true
  loadError.value = null
  model.value = null
  validationErrors.value = []
  relativePath.value = `ontology/${name}.yaml`
  activeTab.value = 'overview'
  try {
    const result = await ontologyStore.fetchOntologyModel(ws.path, name)
    if (!result) {
      loadError.value = 'notFound'
      return
    }
    model.value = result.model
    validationErrors.value = result.validation_errors
  } catch (e) {
    console.error('[OntologyDetail] load failed:', e)
    loadError.value = 'unknown'
  } finally {
    isLoading.value = false
  }
}

onMounted(() => load(props.name))
watch(
  () => props.name,
  (n) => load(n),
)
</script>

<template>
  <div class="ontology-detail">
    <!-- Header -->
    <header class="detail-header">
      <div class="title-row">
        <t-icon name="root-list" size="20px" class="title-icon" />
        <h2 class="title">{{ name }}</h2>

        <span v-if="model?.version" class="version-tag">v{{ model.version }}</span>

        <!-- Validation status badge -->
        <span
          v-if="model && errorCount === 0 && warningCount === 0"
          class="status-badge ok"
        >
          <t-icon name="check-circle-filled" size="13px" />
          {{ t('models.ontology.valid') }}
        </span>
        <span v-else-if="model && errorCount > 0" class="status-badge error">
          <t-icon name="error-circle-filled" size="13px" />
          {{ t('models.ontology.errorsCount', { n: errorCount }) }}
        </span>
        <span v-else-if="model && warningCount > 0" class="status-badge warn">
          <t-icon name="error-circle" size="13px" />
          {{ t('models.ontology.warningsCount', { n: warningCount }) }}
        </span>
        <span v-else-if="errorCount > 0" class="status-badge error">
          <t-icon name="error-circle-filled" size="13px" />
          {{ t('models.ontology.errorsCount', { n: errorCount }) }}
        </span>
      </div>
      <p class="path">{{ relativePath }}</p>
    </header>

    <!-- States -->
    <div v-if="isLoading" class="state-block">
      <t-icon name="loading" size="16px" class="spinning" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <div v-else-if="loadError === 'notFound'" class="state-block error">
      <t-icon name="error-circle" size="16px" />
      <span>{{ t('models.ontology.notFound') }}</span>
    </div>

    <div v-else-if="loadError === 'unknown'" class="state-block error">
      <t-icon name="error-circle" size="16px" />
      <span>{{ t('common.error') }}</span>
    </div>

    <!-- Schema-invalid YAML: the model could not be parsed, but the backend
         reports exactly which fields are wrong — show them instead of a
         generic error so the user (or agent) can fix the file. -->
    <div v-else-if="!model" class="schema-invalid">
      <div class="state-block error schema-invalid-head">
        <t-icon name="error-circle-filled" size="16px" />
        <span>
          {{ t('models.ontology.schemaInvalid') }} —
          {{ t('models.ontology.errorsCount', { n: errorCount }) }}
        </span>
      </div>
      <ul class="issue-list">
        <li
          v-for="(err, i) in validationErrors"
          :key="i"
          class="issue"
          :class="err.severity"
        >
          <t-icon
            :name="err.severity === 'error' ? 'error-circle-filled' : 'error-circle'"
            size="13px"
          />
          <div class="issue-body">
            <code class="issue-path">{{ err.path }}</code>
            <span class="issue-msg">{{ err.message }}</span>
          </div>
        </li>
      </ul>
    </div>

    <template v-else-if="model">
      <!-- Tabs -->
      <div class="detail-tabs">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          :class="{ active: activeTab === tab.key }"
          @click="activeTab = tab.key"
        >
          <t-icon :name="tab.icon" size="15px" />
          {{ tab.label }}
          <span v-if="tab.key === 'overview' && entityCount" class="tab-count">{{ entityCount }}</span>
          <span v-if="tab.key === 'metrics' && metricCount" class="tab-count">{{ metricCount }}</span>
        </button>
      </div>

      <!-- Tab content -->
      <div class="detail-content">
        <OntologyOverviewTab
          v-show="activeTab === 'overview'"
          :model="model"
          :validation-errors="validationErrors"
        />
        <OntologyGraphTab v-if="activeTab === 'graph'" :model="model" />
        <OntologyMetricsTab v-show="activeTab === 'metrics'" :model="model" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.ontology-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.detail-header {
  padding: 16px 24px;
  border-bottom: 1px solid var(--td-component-stroke);
  flex-shrink: 0;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.title-icon {
  color: var(--td-brand-color);
  flex-shrink: 0;
}

.title {
  margin: 0;
  font-size: var(--td-font-size-title-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.version-tag {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-secondarycontainer);
  border-radius: 4px;
  font-family: var(--td-font-family-mono);
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 500;
  border-radius: 4px;
}

/* ── Schema-invalid state ── */
.schema-invalid {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px;
}

.schema-invalid-head {
  margin-bottom: 12px;
}

.issue-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.issue {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  font-size: 12px;
}

.issue.error {
  border-color: var(--td-error-color-3);
}

.issue.warning {
  border-color: var(--td-warning-color-3);
}

.issue-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.issue-path {
  font-family: var(--td-font-family-mono);
  color: var(--td-text-color-primary);
  word-break: break-all;
}

.issue-msg {
  color: var(--td-text-color-secondary);
  word-break: break-word;
}

.status-badge.ok {
  color: var(--td-success-color);
  background: var(--td-success-color-1, rgba(0, 168, 112, 0.1));
}

.status-badge.error {
  color: var(--td-error-color);
  background: var(--td-error-color-1, rgba(215, 63, 63, 0.1));
}

.status-badge.warn {
  color: var(--td-warning-color);
  background: var(--td-warning-color-1, rgba(237, 123, 43, 0.1));
}

.path {
  margin: 4px 0 0 28px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
  font-family: var(--td-font-family-mono);
}

.detail-tabs {
  display: flex;
  gap: 2px;
  padding: 0 20px;
  border-bottom: 1px solid var(--td-component-stroke);
  flex-shrink: 0;
}

.detail-tabs button {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  border: none;
  background: transparent;
  border-bottom: 2px solid transparent;
  font-size: var(--td-font-size-body-medium);
  color: var(--td-text-color-secondary);
  cursor: pointer;
  transition: all 0.15s;
}

.detail-tabs button.active {
  color: var(--td-brand-color);
  border-bottom-color: var(--td-brand-color);
  font-weight: 600;
}

.detail-tabs button:hover:not(.active) {
  color: var(--td-text-color-primary);
}

.tab-count {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  border-radius: 9px;
  background: var(--td-bg-color-secondarycontainer);
  color: var(--td-text-color-secondary);
}

.detail-tabs button.active .tab-count {
  background: var(--td-brand-color-light);
  color: var(--td-brand-color);
}

.detail-content {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.state-block {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-small);
}

.state-block.error {
  color: var(--td-error-color);
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
</style>
