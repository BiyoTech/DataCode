<script setup lang="ts">
/**
 * AbiCatalogTreeItem — recursive node renderer for the ad-hoc data-catalog tree.
 *
 * Node kinds and interactions:
 *   folder : expandable catalog/schema group (single click toggles).
 *   table  : expandable model leaf. Expanding lazily loads its columns.
 *            On hover it exposes: preview (eye), insert-name and copy-name.
 *            Double click still injects a full SELECT template.
 *   column : leaf showing a type badge + name + data-type label. On hover it
 *            exposes insert-name and copy-name. The row tooltip shows comments.
 *
 * All heavy actions bubble up (toggle/select/open/insert/copy/preview); this
 * component stays presentational. Kept as a standalone SFC (mirroring
 * ModelTreeItem) because the app bundles the runtime-only Vue build.
 */
import { useI18n } from 'vue-i18n'
import type { CatalogNode } from '@/stores/adhoc'

const props = defineProps<{
  node: CatalogNode
  depth: number
  expandedKeys: Set<string>
  selectedKey?: string | null
}>()

const emit = defineEmits<{
  (e: 'toggle', node: CatalogNode): void
  (e: 'select', node: CatalogNode): void
  (e: 'open', node: CatalogNode): void
  (e: 'insert', node: CatalogNode): void
  (e: 'copy', node: CatalogNode): void
  (e: 'preview', node: CatalogNode): void
}>()

const { t } = useI18n()

// Numeric-ish data types get the "123" badge; everything else gets "ABC".
const NUMERIC_RE = /(int|long|float|double|decimal|numeric|number|real|serial|bigint|smallint|tinyint)/i

function isNumeric(type?: string): boolean {
  return !!type && NUMERIC_RE.test(type)
}

function isExpanded(): boolean {
  return props.expandedKeys.has(props.node.key)
}

// Hover tooltip text: prefer comment, fall back to a helpful identifier.
function rowTitle(): string {
  const n = props.node
  if (n.comment) return n.comment
  if (n.type === 'table') return n.fqn || n.name
  if (n.type === 'column') return n.dataType ? `${n.name} : ${n.dataType}` : n.name
  return n.name
}

function handleClick() {
  if (props.node.type === 'column') {
    emit('select', props.node)
  } else {
    emit('toggle', props.node)
  }
}

function handleDblClick() {
  if (props.node.type === 'table') emit('open', props.node)
}
</script>

<template>
  <div>
    <div
      class="tree-item"
      :class="{
        leaf: node.type === 'column',
        selected: selectedKey === node.key,
      }"
      :style="{ paddingLeft: depth * 16 + 8 + 'px' }"
      :title="rowTitle()"
      @click="handleClick"
      @dblclick="handleDblClick"
    >
      <!-- Chevron for expandable folders / tables -->
      <t-icon
        v-if="node.type !== 'column'"
        :name="isExpanded() ? 'chevron-down' : 'chevron-right'"
        size="12px"
        class="chevron-icon"
      />
      <span v-else class="chevron-spacer" />

      <!-- Leading type indicator -->
      <t-icon
        v-if="node.loading"
        name="loading"
        size="14px"
        class="item-icon spinning"
      />
      <t-icon
        v-else-if="node.type === 'folder'"
        :name="isExpanded() ? 'folder-open' : 'folder'"
        size="14px"
        class="item-icon"
      />
      <t-icon
        v-else-if="node.type === 'table'"
        name="table"
        size="14px"
        class="item-icon"
      />
      <span
        v-else
        class="type-badge"
        :class="isNumeric(node.dataType) ? 'numeric' : 'text'"
      >
        {{ isNumeric(node.dataType) ? '123' : 'ABC' }}
      </span>

      <span class="item-name">{{ node.name }}</span>

      <!-- Column data type label -->
      <span v-if="node.type === 'column' && node.dataType" class="type-label">
        {{ node.dataType }}
      </span>

      <!-- Hover actions -->
      <span class="item-actions">
        <button
          v-if="node.type === 'table'"
          class="action-btn"
          :title="t('abi.adhoc.catalog.preview')"
          @click.stop="emit('preview', node)"
        >
          <t-icon name="browse" size="14px" />
        </button>
        <button
          v-if="node.type === 'table' || node.type === 'column'"
          class="action-btn"
          :title="t('abi.adhoc.catalog.insertName')"
          @click.stop="emit('insert', node)"
        >
          <t-icon name="add-circle" size="14px" />
        </button>
        <button
          v-if="node.type === 'table' || node.type === 'column'"
          class="action-btn"
          :title="t('abi.adhoc.catalog.copyName')"
          @click.stop="emit('copy', node)"
        >
          <t-icon name="file-copy" size="14px" />
        </button>
      </span>
    </div>

    <!-- Children (folder groups or table columns) -->
    <template v-if="node.type !== 'column' && isExpanded()">
      <AbiCatalogTreeItem
        v-for="child in node.children"
        :key="child.key"
        :node="child"
        :depth="depth + 1"
        :expanded-keys="expandedKeys"
        :selected-key="selectedKey"
        @toggle="emit('toggle', $event)"
        @select="emit('select', $event)"
        @open="emit('open', $event)"
        @insert="emit('insert', $event)"
        @copy="emit('copy', $event)"
        @preview="emit('preview', $event)"
      />
      <div
        v-if="node.type === 'table' && node.loaded && node.children.length === 0"
        class="tree-empty-child"
        :style="{ paddingLeft: (depth + 1) * 16 + 8 + 'px' }"
      >
        —
      </div>
    </template>
  </div>
</template>

<style scoped>
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

.chevron-spacer {
  width: 14px;
  flex-shrink: 0;
}

.item-icon {
  flex-shrink: 0;
  color: var(--td-text-color-secondary);
}

.tree-item.selected .item-icon {
  color: var(--td-brand-color);
}

/* Column type badge ("123" / "ABC") */
.type-badge {
  flex-shrink: 0;
  width: 22px;
  text-align: center;
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: 0.5px;
  color: var(--td-text-color-placeholder);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.type-badge.numeric {
  color: var(--td-warning-color, #e37318);
}

.type-badge.text {
  color: var(--td-success-color, #2ba471);
}

.item-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Column data-type label on the right */
.type-label {
  margin-left: auto;
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--td-bg-color-component, rgba(0, 0, 0, 0.04));
  color: var(--td-text-color-secondary);
  font-size: 11px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

/* Hover action buttons */
.item-actions {
  margin-left: auto;
  display: none;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}

/* When a type label is present it already pushed to the right, so the
   actions should sit right after it without another auto margin. */
.type-label + .item-actions {
  margin-left: 6px;
}

.tree-item:hover .item-actions {
  display: flex;
}

/* Hide the type label on hover to make room for the action buttons. */
.tree-item:hover .type-label {
  display: none;
}

.action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--td-text-color-secondary);
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  transition: background 0.1s, color 0.1s;
}

.action-btn:hover {
  background: var(--td-bg-color-container-active, rgba(0, 0, 0, 0.08));
  color: var(--td-brand-color);
}

.tree-empty-child {
  padding: 4px 8px;
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
