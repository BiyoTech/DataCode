<script setup lang="ts">
/**
 * PlanChangePreview - Displays categorized model changes from plan preview.
 * Renders Added/Removed/Direct/Indirect/Metadata change cards with optional diff.
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '@/stores/plan'
import type { PlanChange } from '@/types/plan'

const { t } = useI18n()
const planStore = usePlanStore()

// Track expanded diffs
const expandedDiffs = ref<Set<string>>(new Set())

function getDiffLineClass(line: string): string {
  if (line.startsWith('+')) return 'diff-add'
  if (line.startsWith('-')) return 'diff-remove'
  if (line.startsWith('@@')) return 'diff-hunk'
  return ''
}

function toggleDiff(name: string) {
  if (expandedDiffs.value.has(name)) {
    expandedDiffs.value.delete(name)
  } else {
    expandedDiffs.value.add(name)
  }
}

function getDisplayName(change: PlanChange): string {
  return change.view_name || change.display_view_name || change.name
}
</script>

<template>
  <div v-if="planStore.isPreview || planStore.isDone" class="change-preview">
    <!-- No changes -->
    <div v-if="!planStore.hasChanges" class="no-changes">
      <t-icon name="check-circle" size="16px" style="margin-right:4px;vertical-align:middle" /><span>{{ t('plan.noChanges') }}</span>
    </div>

    <template v-else>
      <!-- Added models -->
      <div v-if="planStore.changes.added.length > 0" class="change-group added">
        <div class="group-header">
          <t-icon name="add" size="12px" />
          <span>{{ t('plan.added') }} ({{ planStore.changes.added.length }})</span>
        </div>
        <ul class="change-list">
          <li v-for="change in planStore.changes.added" :key="change.name" class="change-item">
            <span class="change-name">{{ getDisplayName(change) }}</span>
          </li>
        </ul>
      </div>

      <!-- Removed models -->
      <div v-if="planStore.changes.removed.length > 0" class="change-group removed">
        <div class="group-header">
          <t-icon name="minus" size="12px" />
          <span>{{ t('plan.removed') }} ({{ planStore.changes.removed.length }})</span>
        </div>
        <ul class="change-list">
          <li v-for="change in planStore.changes.removed" :key="change.name" class="change-item">
            <span class="change-name">{{ getDisplayName(change) }}</span>
          </li>
        </ul>
      </div>

      <!-- Direct modifications -->
      <div v-if="planStore.changes.direct.length > 0" class="change-group direct">
        <div class="group-header">
          <t-icon name="swap" size="12px" />
          <span>{{ t('plan.directModified') }} ({{ planStore.changes.direct.length }})</span>
        </div>
        <ul class="change-list">
          <li
            v-for="change in planStore.changes.direct"
            :key="change.name"
            class="change-item expandable"
          >
            <button class="change-item-header" @click="toggleDiff(change.name)">
              <t-icon
                :name="expandedDiffs.has(change.name) ? 'chevron-down' : 'chevron-right'"
                size="10px"
              />
              <span class="change-name">{{ getDisplayName(change) }}</span>
            </button>
            <!-- Diff content -->
            <div v-if="expandedDiffs.has(change.name) && change.diff" class="diff-block">
              <pre class="diff-content"><template v-for="(line, idx) in change.diff.split('\n')" :key="idx"><span :class="getDiffLineClass(line)">{{ line }}
</span></template></pre>
            </div>
          </li>
        </ul>
      </div>

      <!-- Indirect modifications -->
      <div v-if="planStore.changes.indirect.length > 0" class="change-group indirect">
        <div class="group-header">
          <t-icon name="git-branch" size="12px" />
          <span>{{ t('plan.indirectModified') }} ({{ planStore.changes.indirect.length }})</span>
        </div>
        <ul class="change-list">
          <li v-for="change in planStore.changes.indirect" :key="change.name" class="change-item">
            <span class="change-name">{{ getDisplayName(change) }}</span>
          </li>
        </ul>
      </div>

      <!-- Metadata changes -->
      <div v-if="planStore.changes.metadata.length > 0" class="change-group metadata">
        <div class="group-header">
          <t-icon name="file" size="12px" />
          <span>{{ t('plan.metadataModified') }} ({{ planStore.changes.metadata.length }})</span>
        </div>
        <ul class="change-list">
          <li v-for="change in planStore.changes.metadata" :key="change.name" class="change-item">
            <span class="change-name">{{ getDisplayName(change) }}</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- Backfills -->
    <div v-if="planStore.backfills.length > 0" class="change-group backfill">
      <div class="group-header">
        <t-icon name="server" size="12px" />
        <span>{{ t('plan.backfills') }} ({{ planStore.backfills.length }})</span>
      </div>
      <ul class="change-list">
        <li v-for="bf in planStore.backfills" :key="bf.name" class="change-item backfill-item">
          <span class="change-name">{{ bf.view_name || bf.name }}</span>
          <span v-if="bf.interval && bf.interval.length" class="backfill-interval">
            {{ bf.interval.join(', ') }}
          </span>
          <span v-if="bf.batches" class="backfill-batches">{{ bf.batches }} {{ t('plan.batches') }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>



<style scoped>
.change-preview {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
}

.no-changes {
  padding: 16px;
  text-align: center;
  color: var(--success);
  font-size: 13px;
  background: var(--success-subtle);
  border: 1px solid var(--success);
  border-radius: 8px;
}

.change-group {
  border-radius: 8px;
  padding: 10px 12px;
  border: 1px solid transparent;
}

.change-group.added {
  background: var(--success-subtle);
  border-color: var(--success);
}

.change-group.removed {
  background: var(--error-subtle);
  border-color: var(--error);
}

.change-group.direct {
  background: var(--accent-subtle);
  border-color: var(--accent);
}

.change-group.indirect {
  background: var(--warning-subtle);
  border-color: var(--warning);
}

.change-group.metadata {
  background: var(--bg-hover);
  border-color: var(--text-muted);
}

.change-group.backfill {
  background: var(--success-subtle);
  border-color: var(--success);
}

.backfill .group-header { color: var(--success); }

.backfill-item {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.backfill-interval {
  font-size: 10px;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}

.backfill-batches {
  font-size: 10px;
  color: var(--success);
  background: var(--success-subtle);
  padding: 1px 6px;
  border-radius: 8px;
}

.group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.added .group-header { color: var(--success); }
.removed .group-header { color: var(--error); }
.direct .group-header { color: var(--accent); }
.indirect .group-header { color: var(--warning); }
.metadata .group-header { color: var(--text-muted); }

.change-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.change-item {
  padding: 3px 0;
  font-size: 12px;
}

.change-name {
  color: var(--text-primary);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
}

.expandable {
  cursor: pointer;
}

.change-item-header {
  display: flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  color: var(--text-primary);
  cursor: pointer;
  padding: 2px 0;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
}

.change-item-header:hover {
  color: var(--accent);
}

.diff-block {
  margin-top: 6px;
  margin-left: 14px;
  background: var(--bg-surface);
  border-radius: 6px;
  overflow: hidden;
}

.diff-content {
  padding: 8px 10px;
  font-size: 10px;
  line-height: 1.5;
  font-family: var(--font-mono, monospace);
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
}

.diff-add {
  color: var(--success);
  background: var(--success-subtle);
}

.diff-remove {
  color: var(--error);
  background: var(--error-subtle);
}

.diff-hunk {
  color: var(--accent);
  margin-top: 4px;
}
</style>
