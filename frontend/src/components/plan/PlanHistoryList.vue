<script setup lang="ts">
/**
 * PlanHistoryList - Timeline-style apply history list.
 * Each card shows: timestamp, user/agent, environment, status, change counts.
 * Clicking a card fetches the full detail and expands an inline detail panel.
 */
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '@/stores/plan'
import { formatRelativeTime } from '@/utils/timezone'
import PlanHistoryDetail from './PlanHistoryDetail.vue'
import type { ApplyHistoryRecord } from '@/types/plan'

const { t } = useI18n()
const planStore = usePlanStore()

function getStatusBadgeClass(record: ApplyHistoryRecord): string {
  if (record.applied) return 'badge-success'
  if (record.reason === 'no_changes') return 'badge-muted'
  if (record.reason === 'tests_failed') return 'badge-warning'
  if (record.error_message) return 'badge-error'
  return 'badge-muted'
}

function getStatusLabel(record: ApplyHistoryRecord): string {
  if (record.applied) return t('plan.historyStatusApplied')
  if (record.reason === 'no_changes') return t('plan.historyStatusNoChanges')
  if (record.reason === 'tests_failed') return t('plan.historyStatusTestsFailed')
  if (record.error_message) return t('plan.historyStatusFailed')
  return record.reason || t('plan.historyStatusFailed')
}

function getTriggerIcon(record: ApplyHistoryRecord): string {
  return record.trigger_type === 'agent' ? 'logo-codepen' : 'user'
}

function getTriggerLabel(record: ApplyHistoryRecord): string {
  if (record.trigger_type === 'agent') return t('plan.triggerAgent')
  if (record.user_id) return record.user_id
  return t('plan.triggerHuman')
}

function getChangeSummary(record: ApplyHistoryRecord): string {
  const parts: string[] = []
  if (record.added_count) parts.push(`+${record.added_count}`)
  if (record.removed_count) parts.push(`-${record.removed_count}`)
  if (record.direct_count) parts.push(`~${record.direct_count}`)
  return parts.length ? parts.join(' ') : ''
}

function isExpanded(recordId: string): boolean {
  return planStore.selectedHistoryId === recordId
}

function toggleDetail(record: ApplyHistoryRecord): void {
  if (isExpanded(record.id)) {
    planStore.closeHistoryDetail()
  } else {
    planStore.fetchHistoryDetail(record.id)
  }
}

function formatCreatedAt(iso: string): string {
  try {
    return formatRelativeTime(iso)
  } catch {
    return iso
  }
}
</script>

<template>
  <div class="history-list">
    <!-- Header -->
    <div class="history-header">
      <span class="history-title">{{ t('plan.tabHistory') }}</span>
      <button class="refresh-btn" :disabled="planStore.historyLoading" @click="planStore.fetchHistory()">
        <t-icon name="refresh" size="12px" :class="{ spinning: planStore.historyLoading }" />
      </button>
    </div>

    <!-- Loading -->
    <div v-if="planStore.historyLoading && planStore.historyRecords.length === 0" class="history-loading">
      <div class="spinner" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- Empty -->
    <div v-else-if="planStore.historyRecords.length === 0" class="history-empty">
      <t-icon name="time" size="24px" class="empty-icon" />
      <p>{{ t('plan.historyEmpty') }}</p>
    </div>

    <!-- Timeline -->
    <div v-else class="timeline">
      <div
        v-for="(record, idx) in planStore.historyRecords"
        :key="record.id"
        class="timeline-row"
      >
        <!-- Time marker -->
        <div class="timeline-marker">
          <div class="marker-dot" :class="record.applied ? 'dot-success' : 'dot-muted'" />
          <div v-if="idx < planStore.historyRecords.length - 1" class="marker-line" />
        </div>

        <!-- Card -->
        <div class="history-card" :class="{ expanded: isExpanded(record.id) }">
          <button class="card-main" @click="toggleDetail(record)">
            <div class="card-left">
              <t-icon :name="getTriggerIcon(record)" size="14px" class="trigger-icon" />
              <span class="trigger-label">{{ getTriggerLabel(record) }}</span>
            </div>
            <div class="card-meta">
              <span class="card-env">
                <t-icon name="layer" size="10px" />
                {{ record.environment }}
              </span>
              <span class="status-badge" :class="getStatusBadgeClass(record)">
                {{ getStatusLabel(record) }}
              </span>
            </div>
            <div class="card-right">
              <span class="card-change-summary">{{ getChangeSummary(record) }}</span>
              <span class="card-time">{{ formatCreatedAt(record.created_at) }}</span>
            </div>
            <t-icon
              :name="isExpanded(record.id) ? 'chevron-down' : 'chevron-right'"
              size="12px"
              class="expand-icon"
            />
          </button>

          <!-- Detail panel (expanded) -->
          <div v-if="isExpanded(record.id)" class="card-detail">
            <PlanHistoryDetail
              v-if="planStore.selectedHistoryDetail && !planStore.historyDetailLoading"
              :detail="planStore.selectedHistoryDetail"
            />
            <div v-else-if="planStore.historyDetailLoading" class="detail-loading">
              <div class="spinner" />
            </div>
          </div>
        </div>
      </div>

      <!-- Load more -->
      <div v-if="planStore.hasMoreHistory" class="load-more-row">
        <button
          class="load-more-btn"
          :disabled="planStore.historyLoading"
          @click="planStore.fetchMoreHistory()"
        >
          <t-icon v-if="planStore.historyLoading" name="loading" size="12px" class="spinning" />
          <span>{{ planStore.historyLoading ? t('common.loading') : t('plan.loadMore') }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.history-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px 8px;
  border-bottom: 1px solid var(--border);
}

.history-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
}

.refresh-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.refresh-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.refresh-btn:disabled {
  opacity: 0.5;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.history-loading,
.history-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 40px 16px;
  color: var(--text-muted);
  font-size: 12px;
}

.empty-icon {
  opacity: 0.4;
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* Timeline */
.timeline {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}

.timeline-row {
  display: flex;
  gap: 0;
  padding: 0 8px;
}

.timeline-marker {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 24px;
  flex-shrink: 0;
  padding-top: 10px;
}

.marker-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}

.dot-success {
  background: var(--success);
}

.dot-muted {
  background: var(--text-muted);
}

.marker-line {
  width: 1px;
  flex: 1;
  min-height: 20px;
  background: var(--border);
  margin-top: 4px;
}

/* Card */
.history-card {
  flex: 1;
  margin-bottom: 8px;
  margin-left: 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-surface);
  overflow: hidden;
  transition: border-color 0.15s;
}

.history-card:hover {
  border-color: var(--accent);
}

.history-card.expanded {
  border-color: var(--accent);
}

.card-main {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  font-family: inherit;
}

.card-left {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.trigger-icon {
  color: var(--text-muted);
  flex-shrink: 0;
}

.trigger-label {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 80px;
}

.card-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
}

.card-env {
  display: flex;
  align-items: center;
  gap: 3px;
  font-size: 10px;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
  background: var(--bg-hover);
  padding: 2px 6px;
  border-radius: 4px;
}

.status-badge {
  font-size: 9px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 8px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

.badge-success {
  background: var(--success-subtle);
  color: var(--success);
}

.badge-muted {
  background: var(--bg-hover);
  color: var(--text-muted);
}

.badge-warning {
  background: var(--warning-subtle);
  color: var(--warning);
}

.badge-error {
  background: var(--error-subtle);
  color: var(--error);
}

.card-right {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.card-change-summary {
  font-size: 10px;
  font-family: var(--font-mono, monospace);
  color: var(--text-secondary);
}

.card-time {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}

.expand-icon {
  color: var(--text-muted);
  flex-shrink: 0;
}

/* Detail */
.card-detail {
  border-top: 1px solid var(--border);
  padding: 0;
  max-height: 360px;
  overflow-y: auto;
}

.detail-loading {
  display: flex;
  justify-content: center;
  padding: 20px;
}

/* Load more */
.load-more-row {
  display: flex;
  justify-content: center;
  padding: 8px 0 16px;
}

.load-more-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.15s;
}

.load-more-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.load-more-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
