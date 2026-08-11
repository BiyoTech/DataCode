<script setup lang="ts">
/**
 * RunHistoryPanel — lists recent pipeline runs with a terminal-style log view.
 *
 * Run timestamps arrive as UTC epoch seconds and are converted to the user's
 * local timezone via formatLocalTime. Expanding a row lazily fetches structured
 * run logs from the service.
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDagsterStore } from '@/stores/dagster'
import { formatLocalTime } from '@/utils/timezone'
import { RUN_STATUS_COLORS } from './assetDagNodeRegister'
import type { RunInfo } from '@/types/dagster'

const { t } = useI18n()
const dagster = useDagsterStore()

const expandedRunId = ref<string | null>(null)

/** Convert a UTC epoch-seconds timestamp to a localized string. */
function fmtTime(ts: number | null): string {
  if (!ts) return '—'
  return formatLocalTime(ts * 1000)
}

/** Human-readable duration between start and end epoch seconds. */
function fmtDuration(run: RunInfo): string {
  if (!run.start_time) return '—'
  const end = run.end_time ?? Math.floor(Date.now() / 1000)
  const secs = Math.max(0, Math.round(end - run.start_time))
  if (secs < 60) return `${secs}s`
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return `${m}m ${s}s`
}

function statusColor(status: string): string {
  return RUN_STATUS_COLORS[(status || 'none').toLowerCase()] || RUN_STATUS_COLORS.none
}

async function toggleExpand(run: RunInfo) {
  if (expandedRunId.value === run.run_id) {
    expandedRunId.value = null
    return
  }
  expandedRunId.value = run.run_id
  if (!dagster.runLogs[run.run_id]) {
    await dagster.fetchRunLogs(run.run_id)
  }
}

function isCancelable(status: string): boolean {
  return ['queued', 'starting', 'started', 'not_started'].includes((status || '').toLowerCase())
}

async function cancel(run: RunInfo) {
  await dagster.cancelRun(run.run_id).catch(() => {})
}
</script>

<template>
  <div class="run-history">
    <div class="rh-header">
      <span class="rh-title">{{ t('assetDag.runHistory') }}</span>
      <t-icon
        name="refresh"
        size="14px"
        class="rh-refresh"
        :class="{ spinning: dagster.runsLoading }"
      />
    </div>

    <div v-if="dagster.runs.length === 0" class="rh-empty">
      {{ t('assetDag.noRuns') }}
    </div>

    <div v-else class="rh-list">
      <div v-for="run in dagster.runs" :key="run.run_id" class="rh-row-wrap">
        <button class="rh-row" @click="toggleExpand(run)">
          <span class="status-dot" :style="{ background: statusColor(run.status) }" />
          <span class="rh-job">{{ run.job_name || t('assetDag.unknownJob') }}</span>
          <span class="rh-status">{{ run.status }}</span>
          <span class="rh-time">{{ fmtTime(run.start_time) }}</span>
          <span class="rh-duration">{{ fmtDuration(run) }}</span>
          <button
            v-if="isCancelable(run.status)"
            class="rh-cancel"
            :title="t('assetDag.cancelRun')"
            @click.stop="cancel(run)"
          >
            <t-icon name="close-circle" size="14px" />
          </button>
          <t-icon
            :name="expandedRunId === run.run_id ? 'chevron-up' : 'chevron-down'"
            size="14px"
            class="rh-chevron"
          />
        </button>

        <!-- Expanded terminal-style logs -->
        <div v-if="expandedRunId === run.run_id" class="rh-logs">
          <div v-if="dagster.runsLoading && !dagster.runLogs[run.run_id]" class="logs-loading">
            <t-icon name="loading" size="14px" class="spinning" />
            <span>{{ t('assetDag.loadingLogs') }}</span>
          </div>
          <div v-else-if="!(dagster.runLogs[run.run_id]?.length)" class="logs-empty">
            {{ t('assetDag.noLogs') }}
          </div>
          <pre v-else class="logs-terminal"><span
            v-for="(log, i) in dagster.runLogs[run.run_id]"
            :key="i"
            class="log-line"
            :class="`level-${(log.level || 'info').toLowerCase()}`"
          >{{ log.timestamp ? formatLocalTime(log.timestamp, 'HH:mm:ss') : '' }} {{ log.step_key ? '[' + log.step_key + ']' : '' }} {{ log.message }}
</span></pre>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-history {
  display: flex;
  flex-direction: column;
  height: 100%;
  color: var(--text-secondary);
}

.rh-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.rh-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.rh-refresh {
  color: var(--text-muted);
  margin-left: auto;
}

.rh-empty {
  padding: 32px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.rh-list {
  flex: 1;
  overflow-y: auto;
}

.rh-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 16px;
  border: none;
  border-bottom: 1px solid var(--border-subtle);
  background: transparent;
  color: inherit;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-base);
}
.rh-row:hover {
  background: var(--bg-hover);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.rh-job {
  flex: 1;
  color: var(--text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rh-status {
  text-transform: capitalize;
  color: var(--text-secondary);
}
.rh-time {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.rh-duration {
  color: var(--text-muted);
  min-width: 48px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.rh-cancel {
  border: none;
  background: transparent;
  color: var(--error);
  cursor: pointer;
  display: flex;
  align-items: center;
}
.rh-chevron {
  color: var(--text-muted);
}

.rh-logs {
  background: var(--bg-hover);
  border-bottom: 1px solid var(--border-subtle);
}
.logs-loading,
.logs-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  color: var(--text-muted);
  font-size: 12px;
}
.logs-terminal {
  margin: 0;
  padding: 12px 16px;
  max-height: 240px;
  overflow: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.log-line {
  display: block;
  color: var(--text-secondary);
}
.level-error {
  color: var(--error);
}
.level-warning {
  color: var(--warning);
}
.level-info {
  color: var(--text-secondary);
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
