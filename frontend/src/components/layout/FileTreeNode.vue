<script setup lang="ts">
/**
 * Recursive file tree node component.
 * Renders a single file/directory and its children recursively.
 *
 * Shows:
 *  - File type icon (lucide icons mapped by extension)
 *  - Git decoration: editor-style colouring of the name plus a one-letter
 *    badge. Directories inherit the decoration of their most significant
 *    descendant, so a collapsed folder still signals inner changes.
 *  - Directory expand/collapse chevron + folder icon
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { FileNode } from '@/stores/files'
import type { GitDecoration } from '@/utils/gitDecoration'
// TDesign icons used via <t-icon> in template

const { t } = useI18n()

const props = defineProps<{
  node: FileNode
  depth: number
  /** Map of path → Git decoration (empty when git is not initialized). */
  gitDecorations?: Record<string, GitDecoration>
}>()

defineEmits<{
  (e: 'toggle-dir', node: FileNode): void
  (e: 'open-file', path: string): void
  (e: 'context-menu', event: MouseEvent, node: FileNode): void
}>()

// ────── File type icon mapping (returns t-icon name) ──────
// Well-known dot-files are matched by FULL NAME: they have no extension, so the
// extension switch below would fall through to the unknown-file icon.
const DOTFILE_ICONS: Record<string, string> = {
  '.gitignore': 'setting',
  '.gitattributes': 'setting',
  '.gitmodules': 'setting',
  '.editorconfig': 'setting',
  '.dockerignore': 'setting',
  '.env': 'lock-on',
}

function getFileIcon(name: string): string {
  const lower = name.toLowerCase()
  if (lower.startsWith('.')) {
    // `.env.local`, `.env.production`, ... share the `.env` treatment.
    if (lower.startsWith('.env')) return DOTFILE_ICONS['.env']
    return DOTFILE_ICONS[lower] || 'file'
  }

  const ext = lower.split('.').pop()
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

// ────── Git decoration for this node ──────
const decoration = computed<GitDecoration | null>(
  () => props.gitDecorations?.[props.node.path] ?? null
)

/** CSS modifier driving the name colour, e.g. `git-modified`. */
const decorationClass = computed(() =>
  decoration.value ? `git-${decoration.value.kind}` : ''
)

/** Localized tooltip: the change kind, plus whether it is staged. */
const decorationTitle = computed(() => {
  const current = decoration.value
  if (!current) return ''
  const kind = t(`git.decoration.${current.kind}`)
  if (current.inherited) return `${t('git.decoration.containsChanges')} · ${kind}`
  const stageLabel = current.staged ? t('git.staged') : t('git.unstaged')
  return `${kind} · ${stageLabel}`
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

      <!-- Name — coloured by Git decoration, like an editor's explorer -->
      <span
        class="node-name"
        :class="[{ directory: node.isDirectory }, decorationClass]"
        :title="decorationTitle || node.name"
      >
        {{ node.name }}
      </span>

      <!-- Git status badge. Inherited (directory rollup) badges are dimmed. -->
      <span
        v-if="decoration"
        class="git-badge"
        :class="[decorationClass, { inherited: decoration.inherited }]"
        :title="decorationTitle"
      >
        {{ decoration.letter }}
      </span>
    </div>

    <!-- Recursive children -->
    <template v-if="node.isDirectory && node.isExpanded && node.children">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :git-decorations="gitDecorations"
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

/* ── Git decorations ──
 * The node name takes the colour of its change kind; hover must not override
 * it, so these rules are declared after `.tree-node:hover`.
 */
.node-name.git-untracked,
.git-badge.git-untracked { color: var(--git-untracked); }

.node-name.git-added,
.git-badge.git-added { color: var(--git-added); }

.node-name.git-modified,
.git-badge.git-modified { color: var(--git-modified); }

.node-name.git-renamed,
.git-badge.git-renamed { color: var(--git-renamed); }

.node-name.git-deleted,
.git-badge.git-deleted { color: var(--git-deleted); }

.node-name.git-conflict,
.git-badge.git-conflict {
  color: var(--git-conflict);
  font-weight: 600;
}

/* A deleted path no longer exists on disk: strike it through. */
.node-name.git-deleted {
  text-decoration: line-through;
}

/* Git status badge — compact coloured label */
.git-badge {
  flex-shrink: 0;
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 700;
  padding: 0 3px;
  border-radius: 3px;
}

/* Rolled up from a descendant: present but visually secondary. */
.git-badge.inherited {
  opacity: 0.5;
  font-weight: 500;
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
