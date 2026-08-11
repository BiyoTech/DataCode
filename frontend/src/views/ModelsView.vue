<script setup lang="ts">
/**
 * ModelsView — SQLMesh models browser with ontology support.
 * Layout: left tabbed sidebar (tables / ontologies) + right detail panel.
 *
 * Tabs in the right panel (Overview | Data Preview | Lineage | Change History
 * | Diff) are only relevant for SQLMesh models. When an ontology is selected
 * the detail panel renders a placeholder until Phase 2 ships the full
 * ontology detail view.
 */
import { onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useModelsStore } from '@/stores/models'
import { useWorkspaceStore } from '@/stores/workspace'
import { useOntologyStore } from '@/stores/ontology'
import { useEnvironmentStore } from '@/stores/environment'
import ModelTree from '@/components/models/ModelTree.vue'
import OntologyDetail from '@/components/models/OntologyDetail.vue'
import ModelOverviewTab from '@/components/models/ModelOverviewTab.vue'
import ModelPreviewTab from '@/components/models/ModelPreviewTab.vue'
import ModelLineageTab from '@/components/models/ModelLineageTab.vue'
import ModelHistoryTab from '@/components/models/ModelHistoryTab.vue'
import ModelDiffTab from '@/components/models/ModelDiffTab.vue'

const { t } = useI18n()
const modelsStore = useModelsStore()
const workspaceStore = useWorkspaceStore()
const ontologyStore = useOntologyStore()
const environmentStore = useEnvironmentStore()

// Fetch models once on mount (workspace may not be loaded yet — the watcher handles that case).
onMounted(() => {
  modelsStore.fetchModels()
})

// Reload models when workspace becomes available or changes.
// Watching the computed `currentWorkspace` catches:
//   (a) workspace list initially loads → null → actual workspace
//   (b) user selects a different workspace
watch(() => workspaceStore.currentWorkspace, (ws) => {
  if (!ws) return
  modelsStore.clearSelection()
  ontologyStore.selectOntology(null)
  modelsStore.fetchModels()
  ontologyStore.fetchOntologies(ws.path)
})

// Reload the model list when the selected SQLMesh virtual environment changes,
// so the tree reflects that environment's promoted models. The selection is
// cleared because the previously selected model may not exist in the new env.
watch(() => environmentStore.currentEnvironment, () => {
  if (!workspaceStore.currentWorkspace) return
  modelsStore.clearSelection()
  modelsStore.fetchModels()
})

const modelTabs = [
  { key: 'overview', label: 'models.tabs.overview' },
  { key: 'preview', label: 'models.tabs.preview' },
  { key: 'lineage', label: 'models.tabs.lineage' },
  { key: 'history', label: 'models.tabs.history' },
  { key: 'diff', label: 'models.tabs.diff' },
] as const

// Selecting an ontology in the sidebar clears any model selection so the
// right panel switches over to the placeholder view cleanly.
function handleSelectOntology(name: string) {
  modelsStore.clearSelection()
  ontologyStore.selectOntology(name)
}
</script>

<template>
  <div class="models-view">
    <!-- Left: Tabbed sidebar (tables / ontologies) -->
    <aside class="models-sidebar">
      <ModelTree @select-ontology="handleSelectOntology" />
    </aside>

    <!-- Right: Detail panel -->
    <main class="models-detail">
      <!-- Ontology placeholder (takes precedence when an ontology is selected) -->
      <template v-if="ontologyStore.selectedOntologyName">
        <OntologyDetail :name="ontologyStore.selectedOntologyName" />
      </template>

      <!-- Model detail with sub-tabs -->
      <template v-else-if="modelsStore.selectedModelName">
        <!-- Tabs -->
        <div class="detail-tabs">
          <button
            v-for="tab in modelTabs"
            :key="tab.key"
            class="tab-btn"
            :class="{ active: modelsStore.activeTab === tab.key }"
            @click="modelsStore.switchTab(tab.key)"
          >
            {{ t(tab.label) }}
          </button>
        </div>

        <!-- Tab content -->
        <div class="detail-content">
          <ModelOverviewTab v-if="modelsStore.activeTab === 'overview'" />
          <ModelPreviewTab v-else-if="modelsStore.activeTab === 'preview'" />
          <ModelLineageTab v-else-if="modelsStore.activeTab === 'lineage'" />
          <ModelHistoryTab v-else-if="modelsStore.activeTab === 'history'" />
          <ModelDiffTab v-else-if="modelsStore.activeTab === 'diff'" />
        </div>
      </template>

      <!-- Empty state -->
      <div v-else class="empty-state">
        <t-icon name="chart-bar" size="40px" class="empty-icon" />
        <p>{{ t('models.selectModel') }}</p>
      </div>
    </main>
  </div>
</template>

<style scoped>
.models-view {
  display: flex;
  height: 100%;
  overflow: hidden;
}

.models-sidebar {
  width: 260px;
  min-width: 200px;
  border-right: 1px solid var(--td-component-stroke);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--td-bg-color-container);
}

.models-detail {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--td-bg-color-container);
}

/* ─── Tabs ─── */
.detail-tabs {
  display: flex;
  gap: 0;
  padding: 0 16px;
  border-bottom: 1px solid var(--td-component-stroke);
  flex-shrink: 0;
}

.tab-btn {
  padding: 12px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--td-text-color-secondary);
  font-size: var(--td-font-size-body-medium);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}

.tab-btn:hover {
  color: var(--td-text-color-primary);
}

.tab-btn.active {
  color: var(--td-brand-color);
  border-bottom-color: var(--td-brand-color);
}

/* ─── Content ─── */
.detail-content {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

/* ─── Empty state ─── */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-medium);
}

.empty-icon {
  opacity: 0.4;
}
</style>
