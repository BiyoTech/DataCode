<script setup lang="ts">
/**
 * Workspace log viewer for the bottom panel.
 *
 * Shows the same NDJSON lines the backend writes under `{workspace}/logs/`,
 * merged across services and followed live. A line is rendered as one terminal
 * row; its structured fields expand on click, because the common case is
 * scanning for the failure and the rare case is reading its payload.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useLogsStore, type LogEntry, type LogLevel } from '@/stores/logs'
import { useWorkspaceStore } from '@/stores/workspace'
import { formatLocalTime } from '@/utils/timezone'

const { t } = useI18n()
const logs = useLogsStore()
const workspaceStore = useWorkspaceStore()

const LEVELS: LogLevel[] = ['debug', 'info', 'warning', 'error', 'critical']

const scroller = ref<HTMLElement | null>(null)
const expanded = ref<Set<string>>(new Set())
/** Auto-follow stops as soon as the user scrolls away from the bottom. */
const following = ref(true)

const hasWorkspace = computed(() => Boolean(workspaceStore.currentWorkspace?.path))

let searchTimer: ReturnType<typeof setTimeout> | null = null

onMounted(() => {
  if (hasWorkspace.value) {
    logs.start()
  }
})

onBeforeUnmount(() => {
  logs.stop()
  if (searchTimer) clearTimeout(searchTimer)
})

watch(
  () => workspaceStore.currentWorkspace?.path,
  (path) => {
    logs.stop()
    if (path) logs.start()
  },
)

watch(
  () => logs.entries.length,
  async () => {
    if (!following.value) return
    await nextTick()
    scrollToBottom()
  },
)

function scrollToBottom() {
  const element = scroller.value
  if (element) element.scrollTop = element.scrollHeight
}

function onScroll() {
  const element = scroller.value
  if (!element) return
  const distanceFromBottom =
    element.scrollHeight - element.scrollTop - element.clientHeight
  following.value = distanceFromBottom < 24
}

function toggleLevel(level: LogLevel) {
  const selected = logs.selectedLevels
  logs.selectedLevels = selected.includes(level)
    ? selected.filter((item) => item !== level)
    : [...selected, level]
  logs.applyFilters()
}

function toggleSource(source: string) {
  const selected = logs.selectedSources
  logs.selectedSources = selected.includes(source)
    ? selected.filter((item) => item !== source)
    : [...selected, source]
  logs.applyFilters()
}

function onSearchInput() {
  // Re-opening the tail on every keystroke would thrash the connection.
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => logs.applyFilters(), 350)
}

function toggleExpanded(entry: LogEntry) {
  const next = new Set(expanded.value)
  if (next.has(entry.id)) {
    next.delete(entry.id)
  } else {
    next.add(entry.id)
  }
  expanded.value = next
}

function hasDetail(entry: LogEntry): boolean {
  return Boolean(entry.fields && Object.keys(entry.fields).length) || Boolean(entry.trace_id)
}

function detailText(entry: LogEntry): string {
  const detail: Record<string, unknown> = { ...(entry.fields ?? {}) }
  if (entry.trace_id) detail.trace_id = entry.trace_id
  if (entry.operation) detail.operation = entry.operation
  if (entry.error_code) detail.error_code = entry.error_code
  if (entry.error_log_id) detail.error_log_id = entry.error_log_id
  return JSON.stringify(detail, null, 2)
}

function timeOf(entry: LogEntry): string {
  return entry.ts ? formatLocalTime(entry.ts, 'HH:mm:ss') : '--:--:--'
}
</script>

<template>
  <div class="log-panel">
    <div class="log-toolbar">
      <div class="filter-group">
        <button
          v-for="level in LEVELS"
          :key="level"
          class="chip"
          :class="[`chip-${level}`, { active: logs.selectedLevels.includes(level) }]"
          @click="toggleLevel(level)"
        >
          {{ t(`logs.level.${level}`) }}
        </button>
      </div>

      <div v-if="logs.sources.length" class="filter-group">
        <button
          v-for="source in logs.sources"
          :key="source"
          class="chip"
          :class="{ active: logs.selectedSources.includes(source) }"
          @click="toggleSource(source)"
        >
          {{ source }}
        </button>
      </div>

      <input
        v-model="logs.search"
        class="log-search"
        type="search"
        :placeholder="t('logs.searchPlaceholder')"
        @input="onSearchInput"
      />

      <div class="toolbar-actions">
        <span class="stream-state" :class="{ live: logs.streaming && !logs.paused }">
          {{ logs.paused ? t('logs.paused') : logs.streaming ? t('logs.live') : t('logs.offline') }}
        </span>
        <button class="icon-btn" :title="t('logs.pause')" @click="logs.togglePause()">
          <t-icon :name="logs.paused ? 'play' : 'pause'" size="14px" />
        </button>
        <button class="icon-btn" :title="t('logs.refresh')" @click="logs.load()">
          <t-icon name="refresh" size="14px" />
        </button>
        <button class="icon-btn" :title="t('logs.clear')" @click="logs.clear()">
          <t-icon name="delete" size="14px" />
        </button>
      </div>
    </div>

    <div v-if="!hasWorkspace" class="log-empty">{{ t('logs.noWorkspace') }}</div>

    <div v-else ref="scroller" class="log-body" @scroll="onScroll">
      <div v-if="logs.loading && !logs.entries.length" class="log-empty">
        {{ t('logs.loading') }}
      </div>
      <template v-else>
        <button v-if="logs.hasMore" class="load-more" @click="logs.loadMore()">
          {{ logs.loadingMore ? t('logs.loading') : t('logs.loadOlder') }}
        </button>

        <div v-if="!logs.entries.length" class="log-empty">{{ t('logs.empty') }}</div>

        <pre v-else class="logs-terminal"><span
          v-for="entry in logs.entries"
          :key="entry.id"
          class="log-line"
          :class="[`level-${entry.level}`, { clickable: hasDetail(entry) }]"
          @click="hasDetail(entry) && toggleExpanded(entry)"
        >{{ timeOf(entry) }} {{ entry.level.toUpperCase().padEnd(8) }} [{{ entry.source }}] {{ entry.event }}<span
          v-if="expanded.has(entry.id)"
          class="log-detail"
        >
{{ detailText(entry) }}</span>
</span></pre>
      </template>
    </div>
  </div>
</template>

<style scoped>
.log-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.log-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: 4px;
}

.chip {
  background: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text-muted);
  font-size: 11px;
  padding: 1px 8px;
  cursor: pointer;
}
.chip:hover {
  color: var(--text-primary);
}
.chip.active {
  border-color: var(--accent);
  color: var(--text-primary);
  background: var(--bg-hover);
}
.chip-warning.active {
  border-color: var(--warning);
}
.chip-error.active,
.chip-critical.active {
  border-color: var(--error);
}

.log-search {
  flex: 1;
  min-width: 120px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-primary);
  font-size: 12px;
  padding: 3px 8px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.stream-state {
  font-size: 11px;
  color: var(--text-muted);
}
.stream-state.live {
  color: var(--success, #00a870);
}

.icon-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 3px 5px;
  border-radius: 4px;
}
.icon-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.log-body {
  flex: 1;
  overflow: auto;
}

.log-empty {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.load-more {
  display: block;
  width: 100%;
  background: none;
  border: none;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-muted);
  font-size: 11px;
  padding: 4px;
  cursor: pointer;
}
.load-more:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

/* Terminal rendering mirrors the run log view so both read the same way. */
.logs-terminal {
  margin: 0;
  padding: 8px 12px;
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
.log-line.clickable {
  cursor: pointer;
}
.log-line.clickable:hover {
  background: var(--bg-hover);
}
.level-debug {
  color: var(--text-muted);
}
.level-info {
  color: var(--text-secondary);
}
.level-warning {
  color: var(--warning);
}
.level-error,
.level-critical {
  color: var(--error);
}
.log-detail {
  display: block;
  color: var(--text-muted);
  padding-left: 16px;
}
</style>
