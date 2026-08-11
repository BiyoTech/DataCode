<script setup lang="ts">
/**
 * AbiCatalogTree — the "Data Catalog" panel of the ad-hoc SQL workbench.
 *
 * Layout mirrors ModelTree.vue: a header (title + refresh) and a search box on
 * top of a collapsible tree. The tree is built from the workspace's SQLMesh
 * models (the project space only), grouped by FQN segments
 * (catalog -> schema -> table).
 *
 * Emits `open` with a ready-to-run SELECT template when a table is
 * double-clicked, so the parent workbench can inject it into the editor.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { MessagePlugin } from 'tdesign-vue-next'
import { useAdhocStore, type CatalogNode } from '@/stores/adhoc'
import { useWorkspaceStore } from '@/stores/workspace'
import AbiCatalogTreeItem from './AbiCatalogTreeItem.vue'

const { t } = useI18n()
const adhoc = useAdhocStore()
const workspaceStore = useWorkspaceStore()

const searchQuery = ref('')
const expandedKeys = ref<Set<string>>(new Set())
const selectedKey = ref<string | null>(null)

const emit = defineEmits<{
  // A ready-to-run SELECT template (double-click a table).
  (e: 'open', sql: string): void
  // A bare identifier to insert into the editor at the cursor.
  (e: 'insert', identifier: string): void
  // Request a row preview for a table node.
  (e: 'preview', node: CatalogNode): void
}>()

// Auto-expand top-level folders on first load for better discoverability.
const seededRoots = new Set<string>()
function _seedRoots(nodes: CatalogNode[]) {
  for (const n of nodes) {
    if (n.type === 'folder' && !seededRoots.has(n.key)) {
      seededRoots.add(n.key)
      expandedKeys.value.add(n.key)
    }
  }
}

// Flat table-leaf filter while searching; otherwise the full tree is shown.
const filteredTables = computed<CatalogNode[] | null>(() => {
  if (!searchQuery.value) return null
  const q = searchQuery.value.toLowerCase()
  const result: CatalogNode[] = []
  const walk = (nodes: CatalogNode[]) => {
    for (const n of nodes) {
      if (n.type === 'table') {
        if (
          n.name.toLowerCase().includes(q) ||
          (n.fqn || '').toLowerCase().includes(q)
        ) {
          result.push(n)
        }
      } else {
        walk(n.children)
      }
    }
  }
  walk(adhoc.tree)
  return result
})

async function _reloadCatalog() {
  await adhoc.loadCatalog()
  _seedRoots(adhoc.tree)
}

onMounted(() => {
  // May run before the workspace list has loaded (e.g. right after a full page
  // reload triggered by switching workspaces). loadCatalog no-ops when no
  // workspace is selected; the watcher below loads once it becomes available.
  _reloadCatalog()
})

// (Re)load the catalog whenever the workspace becomes available or changes.
watch(
  () => workspaceStore.currentWorkspace,
  (ws) => {
    if (!ws) return
    expandedKeys.value.clear()
    selectedKey.value = null
    seededRoots.clear()
    _reloadCatalog()
  },
)

function toggleNode(node: CatalogNode) {
  if (expandedKeys.value.has(node.key)) {
    expandedKeys.value.delete(node.key)
    return
  }
  expandedKeys.value.add(node.key)
  // Fetch columns lazily the first time a table is expanded.
  if (node.type === 'table' && !node.loaded) {
    adhoc.loadColumns(node)
  }
}

function selectNode(node: CatalogNode) {
  selectedKey.value = node.key
}

/** Build a runnable SELECT template for a table leaf and bubble it up.
 *  The FQN is backtick-quoted for safe SQL insertion. */
function openNode(node: CatalogNode) {
  if (node.type !== 'table' || !node.fqn) return
  selectedKey.value = node.key
  const sql = `SELECT *\nFROM ${backtickFqn(node.fqn)}\nLIMIT 100;`
  emit('open', sql)
}

/** The clean identifier a node contributes for copy: FQN for tables, plain
 * name for columns (no quoting). */
function copyIdentifier(node: CatalogNode): string {
  return node.type === 'table' ? node.fqn || node.name : node.name
}

/** Wrap each dot-segment of an FQN in backticks for safe SQL insertion
 * (e.g. `sales.orders` -> `` `sales`.`orders` ``). */
function backtickFqn(fqn: string): string {
  return fqn
    .split('.')
    .map((p) => `\`${p}\``)
    .join('.')
}

/** Insert a node's identifier into the editor at the cursor. Table FQNs are
 * backtick-quoted; column names are inserted as-is. */
function insertNode(node: CatalogNode) {
  const text =
    node.type === 'table' && node.fqn ? backtickFqn(node.fqn) : node.name
  emit('insert', text)
}

/** Copy a node's clean identifier to the clipboard with a brief confirmation. */
async function copyNode(node: CatalogNode) {
  const text = copyIdentifier(node)
  try {
    await navigator.clipboard.writeText(text)
    MessagePlugin.success(t('abi.adhoc.catalog.copied'))
  } catch (e) {
    console.error('Copy to clipboard failed:', e)
    MessagePlugin.error(t('abi.adhoc.catalog.copyFailed'))
  }
}

/** Bubble a table preview request up to the workbench. */
function previewNode(node: CatalogNode) {
  if (node.type !== 'table') return
  emit('preview', node)
}

async function refresh() {
  expandedKeys.value.clear()
  selectedKey.value = null
  seededRoots.clear()
  await adhoc.refreshCatalog()
  _seedRoots(adhoc.tree)
}
</script>

<template>
  <div class="catalog-tree">
    <!-- Header -->
    <div class="catalog-header">
      <span class="catalog-title">{{ t('abi.adhoc.catalog.title') }}</span>
      <button class="header-btn" :title="t('abi.adhoc.catalog.refresh')" @click="refresh">
        <t-icon name="refresh" size="16px" />
      </button>
    </div>

    <!-- Search -->
    <div class="tree-search">
      <t-icon name="search" size="14px" class="search-icon" />
      <input
        v-model="searchQuery"
        :placeholder="t('abi.adhoc.catalog.searchPlaceholder')"
        class="search-input"
      />
    </div>

    <!-- Loading -->
    <div v-if="adhoc.catalogLoading" class="tree-loading">
      <t-icon name="loading" size="16px" class="spinning" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- Error -->
    <div v-else-if="adhoc.catalogError" class="tree-error">
      <t-icon name="error-circle" size="14px" />
      <span>{{ adhoc.catalogError }}</span>
    </div>

    <!-- Search results (flat table list) -->
    <div v-else-if="filteredTables" class="tree-list">
      <AbiCatalogTreeItem
        v-for="node in filteredTables"
        :key="node.key"
        :node="node"
        :depth="0"
        :expanded-keys="expandedKeys"
        :selected-key="selectedKey"
        @toggle="toggleNode"
        @select="selectNode"
        @open="openNode"
        @insert="insertNode"
        @copy="copyNode"
        @preview="previewNode"
      />
      <div v-if="filteredTables.length === 0" class="tree-empty">
        {{ t('abi.adhoc.catalog.empty') }}
      </div>
    </div>

    <!-- Full model tree -->
    <div v-else class="tree-list">
      <AbiCatalogTreeItem
        v-for="node in adhoc.tree"
        :key="node.key"
        :node="node"
        :depth="0"
        :expanded-keys="expandedKeys"
        :selected-key="selectedKey"
        @toggle="toggleNode"
        @select="selectNode"
        @open="openNode"
        @insert="insertNode"
        @copy="copyNode"
        @preview="previewNode"
      />
      <div v-if="adhoc.tree.length === 0" class="tree-empty">
        {{ t('abi.adhoc.catalog.empty') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.catalog-tree {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--bg-secondary);
}

.catalog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 8px;
}

.catalog-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--td-text-color-primary);
}

.header-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--td-text-color-secondary);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
  transition: background 0.1s, color 0.1s;
}

.header-btn:hover {
  background: var(--td-bg-color-container-hover);
  color: var(--td-brand-color);
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

.tree-loading,
.tree-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.tree-error {
  color: var(--td-error-color);
}

.tree-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  scrollbar-width: thin;
}

.tree-empty {
  padding: 16px;
  text-align: center;
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
</style>
