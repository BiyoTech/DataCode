<script setup lang="ts">
/**
 * ModelTreeItem — recursive node renderer for the SQLMesh model tree.
 *
 * Extracted into a standalone SFC (instead of a runtime string template) because
 * the app bundles the runtime-only Vue build, which cannot compile string
 * templates at runtime. A precompiled SFC renders correctly in all builds.
 */
import type { ModelTreeNode } from '@/types/models'

const props = defineProps<{
  node: ModelTreeNode
  depth: number
  expandedNodes: Set<string>
  selectedName?: string | null
}>()

const emit = defineEmits<{
  (e: 'toggle', node: ModelTreeNode): void
}>()

function isExpanded(): boolean {
  return props.expandedNodes.has(props.node.fullName)
}

function handleClick() {
  emit('toggle', props.node)
}
</script>

<template>
  <div>
    <div
      class="tree-item"
      :class="{
        leaf: !node.isDirectory,
        selected: !node.isDirectory && selectedName === node.model?.name,
      }"
      :style="{ paddingLeft: depth * 16 + 8 + 'px' }"
      @click="handleClick"
    >
      <t-icon
        v-if="node.isDirectory"
        :name="isExpanded() ? 'chevron-down' : 'chevron-right'"
        size="12px"
        class="chevron-icon"
      />
      <t-icon
        :name="node.isDirectory ? (isExpanded() ? 'folder-open' : 'folder') : 'file'"
        size="14px"
        class="item-icon"
      />
      <span class="item-name">{{ node.name }}</span>
    </div>

    <template v-if="node.isDirectory && isExpanded() && node.children">
      <ModelTreeItem
        v-for="child in node.children"
        :key="child.fullName"
        :node="child"
        :depth="depth + 1"
        :expanded-nodes="expandedNodes"
        :selected-name="selectedName"
        @toggle="emit('toggle', $event)"
      />
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
</style>
