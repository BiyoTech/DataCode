<script setup lang="ts">
/**
 * Recursive file tree node component.
 * Renders a single file/directory and its children recursively.
 *
 * Shows:
 *  - File type icon (lucide icons mapped by extension)
 *  - Git status badge (M/A/D/R/U as coloured labels)
 *  - Directory expand/collapse chevron + folder icon
 */
import { computed } from 'vue'
import type { FileNode } from '@/stores/files'
// TDesign icons used via <t-icon> in template

export interface GitFileStatus {
  status: string   // M | A | D | R | U | ?
  staged: boolean
}

const props = defineProps<{
  node: FileNode
  depth: number
  /** Map of file path → Git status (optional, only populated when git is initialized). */
  gitStatusMap?: Record<string, GitFileStatus>
}>()

const emit = defineEmits<{
  (e: 'toggle-dir', node: FileNode): void
  (e: 'open-file', path: string): void
  (e: 'context-menu', event: MouseEvent, node: FileNode): void
}>()

// ────── File type icon mapping (returns t-icon name) ──────
function getFileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase()
  switch (ext) {
    case 'sql': return 'server'
    case 'py': case 'ts': case 'js': case 'tsx': case 'jsx': return 'code'
    case 'yaml': case 'yml': return 'setting'
    case 'json': return 'file-excel'
    case 'md': case 'markdown': return 'file'
    default: return 'file-unknown'
  }
}

// ────── Preset directory icons ──────
// Workspace follows a dual-directory layout:
//   {workspace}/
//     sql-warehouse/   ← SQLMesh project (data modeling / ELT / quality)
//     asset-dag/       ← Dagster project (Data+AI pipeline orchestration)
//     ontology/        ← semantic layer
//     design/          ← design artifacts
//     errors/          ← error outputs
//     venv/            ← local Python virtual environment
//
// Each well-known directory gets a semantic icon so its purpose is recognizable
// at a glance. Top-level keys (matched by full path) cover workspace roots; the
// nested maps cover SQLMesh and Dagster sub-directories one level deep.
const PRESET_TOP_ICONS: Record<string, string> = {
  asset_dag: 'chart-bubble',      // Dagster Asset DAG project root
  design: 'palette',              // design artifacts
  errors: 'error-circle',         // error outputs
  ontology: 'relativity',         // semantic layer / entity relationships
  sql_warehouse: 'data-base',     // SQLMesh project root
  venv: 'lock-on',                // local Python virtual environment (read-only)
}

const PRESET_SQLWAREHOUSE_ICONS: Record<string, string> = {
  audits: 'secured',            // data quality / audit rules
  external_models: 'cloud-download', // imported external table schemas
  logs: 'system-log',           // SQLMesh run logs
  macros: 'code',               // reusable SQL/Python macros
  models: 'data-base',          // SQLMesh models
  seeds: 'system-storage',      // seed data files
  tests: 'task-checked',        // model tests
}

const PRESET_ASSETDAG_ICONS: Record<string, string> = {
  defs: 'app',                  // Dagster Definitions / asset groups
  resources: 'server',          // Dagster resources (IO managers, executors)
  tests: 'task-checked',        // Dagster asset tests
}

// Resolve the icon for a directory node. Preset icons are matched by parent path
// so they work both at the workspace root and one level inside the SQLMesh /
// Dagster project sub-directories.
function getDirIcon(node: FileNode): string {
  const parentPath = node.path.includes('/')
    ? node.path.slice(0, node.path.lastIndexOf('/'))
    : ''
  const nameKey = node.name.replace(/-/g, '_')

  // Workspace root-level preset
  if (!parentPath) {
    const rootIcon = PRESET_TOP_ICONS[nameKey]
    if (rootIcon) return rootIcon
    return node.isExpanded ? 'folder-open' : 'folder'
  }

  // Nested SQLMesh sub-directory (e.g. sql-warehouse/audits)
  if (parentPath === 'sql-warehouse') {
    const icon = PRESET_SQLWAREHOUSE_ICONS[node.name]
    if (icon) return icon
  }

  // Nested Dagster sub-directory (e.g. asset-dag/defs)
  if (parentPath === 'asset-dag') {
    const icon = PRESET_ASSETDAG_ICONS[node.name]
    if (icon) return icon
  }

  return node.isExpanded ? 'folder-open' : 'folder'
}

// ────── Git status for this node ──────
const gitStatus = computed<GitFileStatus | null>(() => {
  const map = props.gitStatusMap
  if (!map) return null
  return map[props.node.path] || null
})

const gitLabel = computed(() => gitStatus.value?.status || '')

const gitColor = computed(() => {
  switch (gitLabel.value) {
    case 'M': return 'var(--warning)'     // modified
    case 'A': return 'var(--success)'     // added
    case 'D': return 'var(--error)'       // deleted
    case 'R': return 'var(--accent)'      // renamed
    case 'U': case '?': return 'var(--text-muted)' // untracked
    default: return 'var(--text-muted)'
  }
})
</script>

<template>
  <div>
    <!-- Node item -->
    <div
      class="tree-node"
      :style="{ paddingLeft: `${depth * 16 + 8}px` }"
      @click="node.isDirectory ? $emit('toggle-dir', node) : $emit('open-file', node.path)"
      @contextmenu.prevent="$emit('context-menu', $event, node)"
    >
      <!-- Directory chevron -->
      <span v-if="node.isDirectory" class="node-chevron">
        <t-icon v-if="node.isLoading" name="loading" size="12px" class="spinner" />
        <t-icon v-else-if="node.isExpanded" name="chevron-down" size="12px" />
        <t-icon v-else name="chevron-right" size="12px" />
      </span>

      <!-- Directory folder icon (semantic icon for preset root dirs) -->
      <t-icon
        v-if="node.isDirectory"
        :name="getDirIcon(node)"
        size="15px"
        class="folder-icon"
      />

      <!-- File type icon (non-directory) -->
      <t-icon
        v-else
        :name="getFileIcon(node.name)"
        size="14px"
        class="file-type-icon"
      />

      <!-- Name -->
      <span class="node-name" :class="{ directory: node.isDirectory }">
        {{ node.name }}
      </span>

      <!-- Git status badge -->
      <span
        v-if="gitStatus"
        class="git-badge"
        :style="{ color: gitColor, borderColor: gitColor }"
        :title="gitStatus.staged ? 'Staged' : 'Unstaged'"
      >
        {{ gitLabel }}
      </span>
    </div>

    <!-- Recursive children -->
    <template v-if="node.isDirectory && node.isExpanded && node.children">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :git-status-map="gitStatusMap"
        @toggle-dir="$emit('toggle-dir', $event)"
        @open-file="$emit('open-file', $event)"
        @context-menu="(evt: MouseEvent, n: FileNode) => $emit('context-menu', evt, n)"
      />
    </template>
  </div>
</template>

<style scoped>
.tree-node {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-secondary);
  user-select: none;
}

.tree-node:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.node-chevron {
  width: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  flex-shrink: 0;
}

.folder-icon {
  flex-shrink: 0;
  color: var(--accent);
}

.file-type-icon {
  flex-shrink: 0;
  color: var(--text-secondary);
}

.node-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.node-name.directory {
  font-weight: 500;
}

/* Git status badge — compact coloured label */
.git-badge {
  flex-shrink: 0;
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 700;
  padding: 1px 4px;
  border-radius: 10px;
  border: 1px solid;
  opacity: 0.8;
}

.spinner {
  animation: spin 1s linear infinite;
  display: inline-block;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
