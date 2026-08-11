<script setup lang="ts">
/**
 * ScheduleManager — lists schedules for the workspace's code location.
 *
 * Displays each schedule's cron expression, associated pipeline and running
 * state. Schedule mutation (create/toggle) is delegated to the Dagster
 * webserver in the current phase; this panel focuses on visibility.
 */
import { useI18n } from 'vue-i18n'
import { useDagsterStore } from '@/stores/dagster'
import type { ScheduleInfo } from '@/types/dagster'

const { t } = useI18n()
const dagster = useDagsterStore()

function isRunning(s: ScheduleInfo): boolean {
  return (s.status || '').toLowerCase() === 'running'
}
</script>

<template>
  <div class="schedule-mgr">
    <div class="sm-header">
      <span class="sm-title">{{ t('assetDag.schedules') }}</span>
      <t-icon
        name="refresh"
        size="14px"
        class="sm-refresh"
        :class="{ spinning: dagster.schedulesLoading }"
      />
    </div>

    <div v-if="dagster.schedules.length === 0" class="sm-empty">
      {{ t('assetDag.noSchedules') }}
    </div>

    <div v-else class="sm-list">
      <div v-for="s in dagster.schedules" :key="s.name" class="sm-card">
        <div class="sm-card-top">
          <span class="sm-name">{{ s.name }}</span>
          <span class="sm-state" :class="{ on: isRunning(s) }">
            {{ isRunning(s) ? t('assetDag.scheduleOn') : t('assetDag.scheduleOff') }}
          </span>
        </div>
        <div class="sm-cron">
          <t-icon name="time" size="12px" />
          <code>{{ s.cron_schedule || '—' }}</code>
        </div>
        <div v-if="s.pipeline_name" class="sm-pipeline">
          <t-icon name="layers" size="12px" />
          <span>{{ s.pipeline_name }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.schedule-mgr {
  display: flex;
  flex-direction: column;
  height: 100%;
  color: var(--text-secondary);
}

.sm-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.sm-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.sm-refresh {
  color: var(--text-muted);
  margin-left: auto;
}

.sm-empty {
  padding: 32px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.sm-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sm-card {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-surface);
}
.sm-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.sm-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sm-state {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.sm-state.on {
  background: var(--success-subtle);
  color: var(--success);
}

.sm-cron,
.sm-pipeline {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.sm-cron code {
  font-family: var(--font-mono);
  color: var(--accent);
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
