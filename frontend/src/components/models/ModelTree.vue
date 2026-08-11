<script setup lang="ts">
/**
 * ModelTree — left sidebar that hosts two tabs:
 *   • tables     → hierarchical tree of SQLMesh models (original behavior)
 *   • ontologies → flat list of user-authored ontologies (YAML under
 *                  `{workspace}/ontology/`)
 *
 * The tab strip is rendered in the row that previously held the workspace
 * label so the change matches the reference UI exactly.
 */
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useModelsStore } from '@/stores/models'
import { useWorkspaceStore } from '@/stores/workspace'
import { useOntologyStore } from '@/stores/ontology'
import type { ModelTreeNode } from '@/types/models'
import ModelTreeItem from './ModelTreeItem.vue'
import OntologyList from './OntologyList.vue'

const { t } = useI18n()
const modelsStore = useModelsStore()
const workspaceStore = useWorkspaceStore()
const ontologyStore = useOntologyStore()

export type SidebarTab = 'tables' | 'ontologies'

const activeTab = ref<SidebarTab>('tables')
const searchQuery = ref('')
const expandedNodes = ref<Set<string>>(new Set())

// Top-level folders already auto-expanded once, so manual collapses are respected.
const seededRoots = new Set<string>()

// Auto-expand top-level folders the first time they appear for better discoverability.
watch(
  () => modelsStore.modelTree,
  (tree) => {
    for (const node of tree) {
      if (node.isDirectory && !seededRoots.has(node.fullName)) {
        seededRoots.add(node.fullName)
        expandedNodes.value.add(node.fullName)
      }
    }
  },
  { immediate: true }
)

// Lazy-load ontologies the first time the user opens the tab.
watch(activeTab, (tab) => {
  if (tab === 'ontologies') {
    const ws = workspaceStore.currentWorkspace
    if (ws) ontologyStore.fetchOntologies(ws.path)
  }
})

// Also pre-fetch ontologies when the workspace becomes available, so the
// first tab switch is instant.
watch(
  () => workspaceStore.currentWorkspace?.path,
  (path) => {
    if (path) ontologyStore.fetchOntologies(path)
  },
  { immediate: true }
)

// Reset cross-tab state when the user switches workspaces.
watch(
  () => workspaceStore.currentWorkspace?.path,
  (path, prev) => {
    if (prev && path !== prev) {
      expandedNodes.value.clear()
      seededRoots.clear()
      ontologyStore.reset()
    }
  }
)

// Filtered flat list when searching; otherwise use tree
const filteredModels = computed(() => {
  if (activeTab.value !== 'tables' || !searchQuery.value) return null
  const q = searchQuery.value.toLowerCase()
  return modelsStore.models.filter(
    m => m.name.toLowerCase().includes(q) || m.fqn.toLowerCase().includes(q)
  )
})

function toggleNode(node: ModelTreeNode) {
  if (node.isDirectory) {
    if (expandedNodes.value.has(node.fullName)) {
      expandedNodes.value.delete(node.fullName)
    } else {
      expandedNodes.value.add(node.fullName)
    }
  } else if (node.model) {
    // Clear ontology selection so the detail panel switches back to the model view.
    // ModelsView gives ontology selection precedence over model selection.
    ontologyStore.selectOntology(null)
    modelsStore.selectModel(node.model.name)
  }
}

function selectFromSearch(modelName: string) {
  // Clear ontology selection so the detail panel switches back to the model view.
  ontologyStore.selectOntology(null)
  modelsStore.selectModel(modelName)
  searchQuery.value = ''
}

function isExpanded(node: ModelTreeNode): boolean {
  return expandedNodes.value.has(node.fullName)
}

function getNodeIcon(node: ModelTreeNode): string {
  if (node.isDirectory) return isExpanded(node) ? 'folder-open' : 'folder'
  return 'file'
}

function switchTab(tab: SidebarTab) {
  if (activeTab.value === tab) return
  activeTab.value = tab
  // Clear the search when crossing tabs so each tab has a clean slate.
  searchQuery.value = ''
}

function handleOntologySelect(name: string) {
  ontologyStore.selectOntology(name)
  emit('select-ontology', name)
}

const emit = defineEmits<{
  (e: 'select-ontology', name: string): void
}>()
</script>

<template>
  <div class="model-tree">
    <!-- Search -->
    <div class="tree-search">
      <t-icon name="search" size="14px" class="search-icon" />
      <input
        v-model="searchQuery"
        :placeholder="activeTab === 'tables' ? t('models.search') : t('models.ontology.search')"
        class="search-input"
      />
    </div>

    <!-- Tab strip: tables / ontologies -->
    <div class="tree-tabs" role="tablist">
      <button
        class="tree-tab"
        :class="{ active: activeTab === 'tables' }"
        role="tab"
        :aria-selected="activeTab === 'tables'"
        @click="switchTab('tables')"
      >
        <t-icon name="view-list" size="14px" />
        <span>{{ t('models.tabs.tables') }}</span>
      </button>
      <button
        class="tree-tab"
        :class="{ active: activeTab === 'ontologies' }"
        role="tab"
        :aria-selected="activeTab === 'ontologies'"
        @click="switchTab('ontologies')"
      >
        <t-icon name="root-list" size="14px" />
        <span>{{ t('models.tabs.ontologies') }}</span>
      </button>
    </div>

    <!-- Workspace label (kept under the tabs for context) -->
    <div class="tree-workspace">
      {{ workspaceStore.currentWorkspace?.name || t('models.title') }}
    </div>

    <!-- Tables tab content -->
    <template v-if="activeTab === 'tables'">
      <!-- Loading -->
      <div v-if="modelsStore.modelsLoading" class="tree-loading">
        <t-icon name="loading" size="16px" class="spinning" />
        <span>{{ t('common.loading') }}</span>
      </div>

      <!-- Search results -->
      <div v-else-if="filteredModels" class="tree-list">
        <div
          v-for="model in filteredModels"
          :key="model.name"
          class="tree-item leaf"
          :class="{ selected: modelsStore.selectedModelName === model.name }"
          @click="selectFromSearch(model.name)"
        >
          <t-icon name="file" size="14px" class="item-icon" />
          <span class="item-name">{{ model.name.split('.').pop() }}</span>
        </div>
        <div v-if="filteredModels.length === 0" class="tree-empty">
          {{ t('models.noModels') }}
        </div>
      </div>

      <!-- Tree -->
      <div v-else class="tree-list">
        <template v-for="node in modelsStore.modelTree" :key="node.fullName">
          <ModelTreeItem
            :node="node"
            :depth="0"
            :expanded-nodes="expandedNodes"
            :selected-name="modelsStore.selectedModelName"
            @toggle="toggleNode"
          />
        </template>
        <div v-if="modelsStore.models.length === 0 && !modelsStore.modelsLoading" class="tree-empty">
          {{ t('models.noModels') }}
        </div>
      </div>
    </template>

    <!-- Ontologies tab content -->
    <template v-else>
      <OntologyList :search-query="searchQuery" @select="handleOntologySelect" />
    </template>
  </div>
</template>

<style scoped>
.model-tree {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.tree-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.search-icon {
  color: var(--td-text-color-placeholder);
  flex-shrink: 0;
}

.search-input {
  flex: 1;
  border: none;
  background: transparent;
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-small);
  outline: none;
}

.search-input::placeholder {
  color: var(--td-text-color-placeholder);
}

/* ─── Tab strip ─── */
.tree-tabs {
  display: flex;
  border-bottom: 1px solid var(--td-component-stroke);
  background: var(--td-bg-color-container);
}

.tree-tab {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--td-text-color-secondary);
  font-size: var(--td-font-size-body-small);
  font-weight: 500;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  white-space: nowrap;
}

.tree-tab:hover {
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-container-hover);
}

.tree-tab.active {
  color: var(--td-brand-color);
  border-bottom-color: var(--td-brand-color);
}

.tree-workspace {
  padding: 8px 12px 4px;
  font-size: var(--td-font-size-body-small);
  font-weight: 600;
  color: var(--td-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.tree-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.tree-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  scrollbar-width: thin;
}

.tree-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 5px 8px;
  cursor: pointer;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-primary);
  transition: background 0.1s;
  user-select: none;
}

.tree-item:hover {
  background: var(--td-bg-color-container-hover);
}

.tree-item.selected {
  background: var(--td-brand-color-light);
  color: var(--td-brand-color);
}

.chevron-icon {
  width: 14px;
  flex-shrink: 0;
  color: var(--td-text-color-secondary);
}

.item-icon {
  flex-shrink: 0;
  color: var(--td-text-color-secondary);
}

.tree-item.selected .item-icon {
  color: var(--td-brand-color);
}

.item-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-empty {
  padding: 16px;
  text-align: center;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}
</style>
