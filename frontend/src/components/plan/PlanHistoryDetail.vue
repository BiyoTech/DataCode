<script setup lang="ts">
/**
 * PlanHistoryDetail - Renders the full plan_result for a history record.
 * Self-contained component: does NOT depend on planStore.
 * Shows categorized changes with expandable SQL diffs, change-category
 * badges, and detailed backfill interval/batch info.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ApplyHistoryDetail, BackfillDetail, PlanChange, PlanResult } from '@/types/plan'

const { t } = useI18n()

const props = defineProps<{
  detail: ApplyHistoryDetail
}>()

// Track which change diffs are expanded (keyed by change name).
const expandedDiffs = ref<Set<string>>(new Set())

function toggleDiff(name: string): void {
  if (expandedDiffs.value.has(name)) {
    expandedDiffs.value.delete(name)
  } else {
    expandedDiffs.value.add(name)
  }
}

function getDisplayName(change: PlanChange | BackfillDetail): string {
  return change.view_name || (change as PlanChange).display_view_name || change.name
}

/** Classify a unified-diff line for +/- / hunk highlighting. */
function getDiffLineClass(line: string): string {
  if (line.startsWith('+')) return 'diff-add'
  if (line.startsWith('-')) return 'diff-remove'
  if (line.startsWith('@@')) return 'diff-hunk'
  return ''
}

/** Map backend change_category enum to a localized, styled badge. */
function getCategoryLabel(category?: string): string {
  switch ((category || '').toUpperCase()) {
    case 'BREAKING': return t('plan.categoryBreaking')
    case 'NON_BREAKING': return t('plan.categoryNonBreaking')
    case 'INDIRECT_BREAKING': return t('plan.categoryIndirectBreaking')
    case 'INDIRECT_NON_BREAKING': return t('plan.categoryIndirectNonBreaking')
    case 'METADATA': return t('plan.categoryMetadata')
    default: return category || ''
  }
}

function getCategoryClass(category?: string): string {
  const c = (category || '').toUpperCase()
  if (c.includes('NON_BREAKING')) return 'cat-non-breaking'
  if (c.includes('BREAKING')) return 'cat-breaking'
  if (c === 'METADATA') return 'cat-metadata'
  return 'cat-default'
}

/** Build flat change groups from PlanResult (same structure as PlanChangePreview uses). */
const groups = computed(() => {
  const planResult: PlanResult | undefined = props.detail.plan_result
  const changes = planResult?.changes
  return {
    added: (changes?.added || []) as PlanChange[],
    removed: (changes?.removed || []) as PlanChange[],
    direct: (changes?.modified?.direct || []) as PlanChange[],
    indirect: (changes?.modified?.indirect || []) as PlanChange[],
    metadata: (changes?.modified?.metadata || []) as PlanChange[],
    backfills: (planResult?.backfills || []) as BackfillDetail[],
    testResults: planResult?.test_results,
    testsFailed: planResult?.tests_failed || false,
  }
})

/** Whether there is anything meaningful to render in the detail body. */
const hasAnyChange = computed(() => {
  const g = groups.value
  return (
    g.added.length > 0 ||
    g.removed.length > 0 ||
    g.direct.length > 0 ||
    g.indirect.length > 0 ||
    g.metadata.length > 0 ||
    g.backfills.length > 0
  )
})
</script>

<template>
  <div class="history-detail">
    <!-- Metadata bar -->
    <div class="detail-meta">
      <span class="detail-env">{{ detail.environment }}</span>
      <span v-if="detail.duration_ms" class="detail-duration">{{ (detail.duration_ms / 1000).toFixed(1) }}s</span>
      <span v-if="detail.error_message" class="detail-error-msg">
        <t-icon name="error-circle" size="10px" />
        {{ detail.error_message }}
      </span>
    </div>

    <div class="detail-changes">
      <template v-if="detail.plan_result && hasAnyChange">
        <!-- Added -->
        <div v-if="groups.added.length > 0" class="change-group added">
          <div class="group-header">
            <t-icon name="add" size="10px" />
            <span>{{ t('plan.added') }} ({{ groups.added.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="change in groups.added" :key="change.name" class="change-item">
              <span class="change-name">{{ getDisplayName(change) }}</span>
            </li>
          </ul>
        </div>

        <!-- Removed -->
        <div v-if="groups.removed.length > 0" class="change-group removed">
          <div class="group-header">
            <t-icon name="minus" size="10px" />
            <span>{{ t('plan.removed') }} ({{ groups.removed.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="change in groups.removed" :key="change.name" class="change-item">
              <span class="change-name">{{ getDisplayName(change) }}</span>
            </li>
          </ul>
        </div>

        <!-- Direct modifications (expandable diff) -->
        <div v-if="groups.direct.length > 0" class="change-group direct">
          <div class="group-header">
            <t-icon name="swap" size="10px" />
            <span>{{ t('plan.directModified') }} ({{ groups.direct.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="change in groups.direct" :key="change.name" class="change-item expandable">
              <button class="change-item-header" @click="toggleDiff(change.name)">
                <t-icon :name="expandedDiffs.has(change.name) ? 'chevron-down' : 'chevron-right'" size="10px" />
                <span class="change-name">{{ getDisplayName(change) }}</span>
                <span
                  v-if="change.change_category"
                  class="category-badge"
                  :class="getCategoryClass(change.change_category)"
                >
                  {{ getCategoryLabel(change.change_category) }}
                </span>
              </button>
              <div v-if="expandedDiffs.has(change.name)" class="diff-block">
                <pre v-if="change.diff" class="diff-content"><template v-for="(line, idx) in change.diff.split('\n')" :key="idx"><span :class="getDiffLineClass(line)">{{ line }}
</span></template></pre>
                <div v-else class="diff-empty">{{ t('plan.historyNoDiff') }}</div>
              </div>
            </li>
          </ul>
        </div>

        <!-- Indirect modifications (expandable diff) -->
        <div v-if="groups.indirect.length > 0" class="change-group indirect">
          <div class="group-header">
            <t-icon name="git-branch" size="10px" />
            <span>{{ t('plan.indirectModified') }} ({{ groups.indirect.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="change in groups.indirect" :key="change.name" class="change-item expandable">
              <button class="change-item-header" @click="toggleDiff(change.name)">
                <t-icon :name="expandedDiffs.has(change.name) ? 'chevron-down' : 'chevron-right'" size="10px" />
                <span class="change-name">{{ getDisplayName(change) }}</span>
                <span
                  v-if="change.change_category"
                  class="category-badge"
                  :class="getCategoryClass(change.change_category)"
                >
                  {{ getCategoryLabel(change.change_category) }}
                </span>
              </button>
              <div v-if="expandedDiffs.has(change.name)" class="diff-block">
                <pre v-if="change.diff" class="diff-content"><template v-for="(line, idx) in change.diff.split('\n')" :key="idx"><span :class="getDiffLineClass(line)">{{ line }}
</span></template></pre>
                <div v-else class="diff-empty">{{ t('plan.historyNoDiff') }}</div>
              </div>
            </li>
          </ul>
        </div>

        <!-- Metadata changes -->
        <div v-if="groups.metadata.length > 0" class="change-group metadata">
          <div class="group-header">
            <t-icon name="file" size="10px" />
            <span>{{ t('plan.metadataModified') }} ({{ groups.metadata.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="change in groups.metadata" :key="change.name" class="change-item">
              <span class="change-name">{{ getDisplayName(change) }}</span>
            </li>
          </ul>
        </div>

        <!-- Backfills (interval ranges + batches) -->
        <div v-if="groups.backfills.length > 0" class="change-group backfill">
          <div class="group-header">
            <t-icon name="server" size="10px" />
            <span>{{ t('plan.backfills') }} ({{ groups.backfills.length }})</span>
          </div>
          <ul class="change-list">
            <li v-for="bf in groups.backfills" :key="bf.name" class="change-item backfill-item">
              <div class="backfill-row">
                <span class="change-name">{{ getDisplayName(bf) }}</span>
                <span v-if="bf.batches" class="backfill-batches">{{ bf.batches }} {{ t('plan.batches') }}</span>
              </div>
              <div v-if="bf.interval && bf.interval.length" class="backfill-intervals">
                <span class="backfill-range-label">{{ t('plan.backfillRange') }}:</span>
                <span v-for="(itv, i) in bf.interval" :key="i" class="backfill-interval">{{ itv }}</span>
              </div>
            </li>
          </ul>
        </div>

        <!-- Test failure info -->
        <div v-if="groups.testsFailed && groups.testResults" class="change-group test-fail">
          <div class="group-header">
            <t-icon name="error-circle" size="10px" />
            <span>{{ t('plan.testsFailedTitle') }} ({{ groups.testResults!.failures }} failures)</span>
          </div>
        </div>
      </template>

      <!-- No changes -->
      <div v-else class="no-result">
        <span>{{ t('plan.noChanges') }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.history-detail {
  padding: 8px 10px;
}

.detail-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.detail-env {
  font-size: 10px;
  font-family: var(--font-mono, monospace);
  color: var(--accent);
  background: var(--accent-subtle);
  padding: 1px 6px;
  border-radius: 4px;
}

.detail-duration {
  font-size: 10px;
  color: var(--text-muted);
}

.detail-error-msg {
  font-size: 10px;
  color: var(--error);
  display: flex;
  align-items: center;
  gap: 3px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-changes {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* Reuse same change-group visual style as PlanChangePreview */
.change-group {
  border-radius: 6px;
  padding: 6px 8px;
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
.change-group.test-fail {
  background: var(--error-subtle);
  border-color: var(--error);
}

.group-header {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 600;
  margin-bottom: 3px;
  text-transform: uppercase;
  letter-spacing: 0.2px;
}

.added .group-header { color: var(--success); }
.removed .group-header { color: var(--error); }
.direct .group-header { color: var(--accent); }
.indirect .group-header { color: var(--warning); }
.metadata .group-header { color: var(--text-muted); }
.backfill .group-header { color: var(--success); }
.test-fail .group-header { color: var(--error); }

.change-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.change-item {
  padding: 2px 0;
  font-size: 10px;
}

.change-name {
  color: var(--text-primary);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

/* Expandable change item with diff */
.expandable {
  cursor: pointer;
}

.change-item-header {
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  background: none;
  border: none;
  color: var(--text-primary);
  cursor: pointer;
  padding: 2px 0;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  text-align: left;
}

.change-item-header:hover {
  color: var(--accent);
}

/* Change category badge */
.category-badge {
  margin-left: auto;
  font-size: 8px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 6px;
  text-transform: uppercase;
  letter-spacing: 0.2px;
  white-space: nowrap;
}

.cat-breaking {
  background: var(--error-subtle);
  color: var(--error);
}
.cat-non-breaking {
  background: var(--success-subtle);
  color: var(--success);
}
.cat-metadata {
  background: var(--bg-hover);
  color: var(--text-muted);
}
.cat-default {
  background: var(--bg-hover);
  color: var(--text-secondary);
}

/* Diff block */
.diff-block {
  margin-top: 6px;
  margin-left: 14px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
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
  max-height: 240px;
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

.diff-empty {
  padding: 8px 10px;
  font-size: 10px;
  color: var(--text-muted);
}

.backfill-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.backfill-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.backfill-intervals {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-left: 2px;
}

.backfill-range-label {
  font-size: 9px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.2px;
}

.backfill-interval {
  font-size: 9px;
  color: var(--text-secondary);
  font-family: var(--font-mono, monospace);
  background: var(--bg-hover);
  padding: 1px 6px;
  border-radius: 4px;
}

.backfill-batches {
  font-size: 9px;
  color: var(--success);
  background: var(--success-subtle);
  padding: 1px 4px;
  border-radius: 6px;
}

.no-result {
  text-align: center;
  padding: 8px;
  color: var(--text-muted);
  font-size: 10px;
}
</style>
