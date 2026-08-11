<script setup lang="ts">
/**
 * AssetDagView — Data+AI Asset Dag workspace view.
 *
 * Layout (dark tech):
 *   ┌───────────────────────────────────────────────────────────┐
 *   │ Header: title · mode badge · workspace · tab switch          │
 *   ├──────────┬────────────────────────────────┬────────────────┤
 *   │ Asset    │   DAG canvas (@antv/g6)         │ Detail panel   │
 *   │ list     │   (or Runs / Schedules panel)   │ (selected)     │
 *   ├──────────┴────────────────────────────────┴────────────────┤
 *   │ Status bar: code location · mode · active runs               │
 *   └───────────────────────────────────────────────────────────┘
 *
 * All data is workspace-scoped and re-fetched when the active workspace
 * changes. Timestamps from the service are UTC; conversion happens in child
 * panels.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { useDagsterStore } from '@/stores/dagster'
import { useEnvironmentStore } from '@/stores/environment'
import PipelineDagGraph from '@/components/asset-dag/PipelineDagGraph.vue'
import AssetListPanel from '@/components/asset-dag/AssetListPanel.vue'
import RunHistoryPanel from '@/components/asset-dag/RunHistoryPanel.vue'
import ScheduleManager from '@/components/asset-dag/ScheduleManager.vue'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const dagster = useDagsterStore()
const environmentStore = useEnvironmentStore()

type CenterTab = 'graph' | 'runs' | 'schedules'
const activeTab = ref<CenterTab>('graph')

const workspacePath = computed(() => workspaceStore.currentWorkspace?.path || '')

const tabs: Array<{ key: CenterTab; label: string; icon: string }> = [
  { key: 'graph', label: 'assetDag.tabGraph', icon: 'chart-bubble' },
  { key: 'runs', label: 'assetDag.tabRuns', icon: 'history' },
  { key: 'schedules', label: 'assetDag.tabSchedules', icon: 'time' },
]

// Number of runs still in-flight (drives the status bar indicator).
const activeRuns = computed(
  () =>
    dagster.runs.filter((r) =>
      ['queued', 'starting', 'started', 'not_started', 'running'].includes(
        (r.status || '').toLowerCase(),
      ),
    ).length,
)

/** Load all workspace-scoped Dagster data for the given path. */
function loadWorkspace(path: string) {
  if (!path) return
  dagster.reset()
  dagster.fetchMode()
  dagster.loadGraph(path)
  dagster.fetchSchedules(path)
}

onMounted(() => {
  loadWorkspace(workspacePath.value)
})

// Reload whenever the active workspace path changes. Watching the primitive
// ``path`` (instead of the workspace object) guarantees the callback fires on
// every switch — the computed object reference can otherwise stay stable and
// silently skip the reload, leaving the graph/list stuck on the first
// workspace until a full backend restart.
watch(
  () => workspaceStore.currentWorkspace?.path,
  (path) => loadWorkspace(path || ''),
)

// Reload the graph when the selected SQLMesh virtual environment changes so the
// asset list + lineage reflect that environment's topology. The subgraph on the
// canvas is rebuilt lazily on the next asset selection (it reads the current
// environment at fetch time), so we only need to refresh the full inventory and
// clear the stale selection here.
watch(
  () => environmentStore.currentEnvironment,
  () => {
    const path = workspacePath.value
    if (!path) return
    dagster.selectAsset(null)
    dagster.loadGraph(path, true)
  },
)
</script>

<template>
  <div class="asset-dag-view">
    <!-- Header -->
    <header class="adv-header">
      <div class="adv-title-group">
        <t-icon name="chart-bubble" size="20px" class="adv-logo" />
        <div>
          <div class="adv-title">{{ t('assetDag.title') }}</div>
          <div class="adv-subtitle">{{ t('assetDag.subtitle') }}</div>
        </div>
      </div>

      <div class="adv-header-right">
        <!-- Workspace name is shown in the top navigation bar; do not repeat it here. -->
        <span class="adv-mode-badge" :class="dagster.isRemote ? 'remote' : 'local'">
          <span class="mode-dot" />
          {{ dagster.isRemote ? t('assetDag.modeRemote') : t('assetDag.modeLocal') }}
        </span>
      </div>
    </header>

    <!-- Tab switch -->
    <nav class="adv-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="adv-tab"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        <t-icon :name="tab.icon" size="14px" />
        {{ t(tab.label) }}
      </button>
    </nav>

    <!-- Body -->
    <div class="adv-body">
      <!-- Graph tab: list + canvas + detail -->
      <template v-if="activeTab === 'graph'">
        <aside class="adv-left">
          <AssetListPanel />
        </aside>

        <main class="adv-center">
          <PipelineDagGraph :workspace-path="workspacePath" />
        </main>

        <aside class="adv-right" :class="{ open: !!dagster.selectedAsset }">
          <div v-if="dagster.selectedAsset" class="detail">
            <div class="detail-header">
              <span class="detail-name">{{ dagster.selectedAsset.name || dagster.selectedAsset.asset_key_str }}</span>
              <button class="detail-close" @click="dagster.selectAsset(null)">
                <t-icon name="close" size="16px" />
              </button>
            </div>

            <div class="detail-meta">
              <span v-if="dagster.selectedAsset.kind" class="meta-chip">
                {{ dagster.selectedAsset.kind }}
              </span>
              <span v-if="dagster.selectedAsset.group_name" class="meta-chip group">
                {{ dagster.selectedAsset.group_name }}
              </span>
            </div>

            <p v-if="dagster.selectedAsset.description" class="detail-desc">
              {{ dagster.selectedAsset.description }}
            </p>

            <div class="detail-section">
              <div class="section-label">
                {{ t('assetDag.upstream') }}
                <span class="section-count">{{ dagster.upstream.length }}</span>
              </div>
              <div v-if="dagster.upstream.length === 0" class="section-empty">
                {{ t('assetDag.none') }}
              </div>
              <button
                v-for="dep in dagster.upstream"
                :key="dep"
                class="dep-item"
                @click="dagster.selectAsset(dep)"
              >
                <t-icon name="arrow-up" size="12px" />
                {{ dagster.displayNameFor(dep) }}
              </button>
            </div>

            <div class="detail-section">
              <div class="section-label">
                {{ t('assetDag.downstream') }}
                <span class="section-count">{{ dagster.downstream.length }}</span>
              </div>
              <div v-if="dagster.downstream.length === 0" class="section-empty">
                {{ t('assetDag.none') }}
              </div>
              <button
                v-for="dep in dagster.downstream"
                :key="dep"
                class="dep-item"
                @click="dagster.selectAsset(dep)"
              >
                <t-icon name="arrow-down" size="12px" />
                {{ dagster.displayNameFor(dep) }}
              </button>
            </div>
          </div>

          <div v-else class="detail-empty">
            <t-icon name="cursor" size="28px" />
            <span>{{ t('assetDag.selectAssetHint') }}</span>
          </div>
        </aside>
      </template>

      <!-- Runs tab -->
      <main v-else-if="activeTab === 'runs'" class="adv-full">
        <RunHistoryPanel />
      </main>

      <!-- Schedules tab -->
      <main v-else class="adv-full">
        <ScheduleManager />
      </main>
    </div>

    <!-- Status bar -->
    <footer class="adv-status">
      <span class="status-item">
        <span class="status-dot ok" />
        {{ t('assetDag.codeLocation') }}: {{ dagster.mode?.code_location_port ?? '—' }}
      </span>
      <span class="status-item">{{ dagster.isRemote ? t('assetDag.modeRemote') : t('assetDag.modeLocal') }}</span>
      <span class="status-item">
        {{ t('assetDag.activeRuns') }}:
        <strong :class="{ warn: activeRuns > 0 }">{{ activeRuns }}</strong>
      </span>
      <span v-if="dagster.errorMsg" class="status-item error">
        <t-icon name="error-circle" size="12px" />
        {{ dagster.errorMsg }}
      </span>
    </footer>
  </div>
</template>

<style scoped>
.asset-dag-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  color: var(--text-primary);
  background: var(--bg-primary);
}

/* ── Header ── */
.adv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
  flex-shrink: 0;
}
.adv-title-group {
  display: flex;
  align-items: center;
  gap: 12px;
}
.adv-logo {
  color: var(--accent);
}
.adv-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}
.adv-subtitle {
  font-size: 12px;
  color: var(--text-muted);
}
.adv-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.adv-mode-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.adv-mode-badge.local {
  background: var(--info-subtle);
  color: var(--info);
  border: 1px solid var(--info);
}
.adv-mode-badge.remote {
  background: var(--warning-subtle);
  color: var(--warning);
  border: 1px solid var(--warning);
}
.mode-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

/* ── Tabs ── */
.adv-tabs {
  display: flex;
  gap: 4px;
  padding: 8px 20px 0;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
  flex-shrink: 0;
}
.adv-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-base);
}
.adv-tab:hover {
  color: var(--text-primary);
}
.adv-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

/* ── Body ── */
.adv-body {
  flex: 1;
  display: flex;
  min-height: 0;
  overflow: hidden;
}
.adv-left {
  width: 260px;
  flex-shrink: 0;
  overflow: hidden;
}
.adv-center {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}
.adv-full {
  flex: 1;
  overflow: hidden;
}

/* ── Right detail panel ── */
.adv-right {
  width: 0;
  overflow: hidden;
  transition: width 0.2s ease;
  border-left: 1px solid transparent;
}
.adv-right.open {
  width: 300px;
  border-left: 1px solid var(--border);
  background: var(--bg-secondary);
}
.detail {
  padding: 16px;
  height: 100%;
  overflow-y: auto;
}
.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.detail-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  word-break: break-all;
}
.detail-close {
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  flex-shrink: 0;
}
.detail-close:hover {
  color: var(--text-primary);
}
.detail-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.meta-chip {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--accent-subtler);
  color: var(--accent);
}
.meta-chip.group {
  background: var(--accent-subtle);
  color: var(--accent-active);
}
.detail-desc {
  margin: 0 0 16px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.detail-section {
  margin-bottom: 16px;
}
.section-label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.section-count {
  padding: 0 7px;
  border-radius: 10px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-weight: 400;
}
.section-empty {
  font-size: 12px;
  color: var(--text-muted);
  padding: 2px 0;
}
.dep-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}
.dep-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.detail-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  height: 100%;
  color: var(--text-muted);
  font-size: 12px;
}

/* ── Status bar ── */
.adv-status {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 6px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg-secondary);
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.status-dot.ok {
  background: var(--success);
}
.status-item strong {
  color: var(--text-secondary);
}
.status-item strong.warn {
  color: var(--warning);
}
.status-item.error {
  color: var(--error);
  margin-left: auto;
}
</style>
