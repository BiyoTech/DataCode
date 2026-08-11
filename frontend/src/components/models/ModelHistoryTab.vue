<script setup lang="ts">
/**
 * ModelHistoryTab — displays the SQLMesh apply history records.
 * Fetches from GET /api/v1/sqlmesh/apply/history and displays as a timeline list.
 */
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { formatRelativeTime } from '@/utils/timezone'
import api from '@/utils/api'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()

interface HistoryRecord {
  id: string
  environment: string
  user_id: string
  trigger_type: string
  applied: boolean
  reason: string | null
  has_changes: boolean
  added_count: number
  removed_count: number
  direct_count: number
  indirect_count: number
  duration_ms: number | null
  error_message: string | null
  created_at: string
}

const records = ref<HistoryRecord[]>([])
const loading = ref(false)
const total = ref(0)

onMounted(() => {
  fetchHistory()
})

async function fetchHistory() {
  const ws = workspaceStore.currentWorkspace
  if (!ws) return

  loading.value = true
  try {
    const { data } = await api.get('/sqlmesh/apply/history', {
      params: { workspace_path: ws.path, limit: 50, offset: 0 },
    })
    records.value = data.records || []
    total.value = data.total || 0
  } catch (e: any) {
    console.error('Failed to fetch apply history:', e)
    records.value = []
  } finally {
    loading.value = false
  }
}

function getStatusClass(record: HistoryRecord): string {
  if (record.applied) return 'status-success'
  if (record.error_message) return 'status-error'
  return 'status-muted'
}

function getStatusLabel(record: HistoryRecord): string {
  if (record.applied) return t('models.historyApplied')
  if (record.reason === 'no_changes') return t('models.historyNoChanges')
  if (record.error_message) return t('models.historyFailed')
  return record.reason || '–'
}
</script>

<template>
  <div class="history-tab">
    <!-- Loading -->
    <div v-if="loading" class="tab-loading">
      <t-icon name="loading" size="20px" class="spinning" />
    </div>

    <!-- Empty -->
    <div v-else-if="records.length === 0" class="history-empty">
      <t-icon name="time" size="24px" class="empty-icon" />
      <p>{{ t('models.historyEmpty') }}</p>
    </div>

    <!-- Records -->
    <div v-else class="history-list">
      <div v-for="record in records" :key="record.id" class="history-card">
        <div class="card-header">
          <span class="card-env">{{ record.environment }}</span>
          <span class="card-badge" :class="getStatusClass(record)">{{ getStatusLabel(record) }}</span>
          <span class="card-time">{{ formatRelativeTime(record.created_at) }}</span>
        </div>
        <div class="card-body">
          <span class="card-user">
            <t-icon :name="record.trigger_type === 'agent' ? 'logo-codepen' : 'user'" size="12px" />
            {{ record.user_id || record.trigger_type }}
          </span>
          <span v-if="record.has_changes" class="card-changes">
            <span v-if="record.added_count" class="change-added">+{{ record.added_count }}</span>
            <span v-if="record.removed_count" class="change-removed">-{{ record.removed_count }}</span>
            <span v-if="record.direct_count" class="change-direct">~{{ record.direct_count }}</span>
          </span>
          <span v-if="record.duration_ms" class="card-duration">
            {{ (record.duration_ms / 1000).toFixed(1) }}s
          </span>
        </div>
        <div v-if="record.error_message" class="card-error">
          {{ record.error_message }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.history-tab {
  padding: 20px 24px;
}

.tab-loading {
  display: flex;
  justify-content: center;
  padding: 40px;
}

.spinning { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.history-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px;
  color: var(--td-text-color-placeholder);
}

.empty-icon { opacity: 0.4; }

.history-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.history-card {
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 12px 14px;
  transition: border-color 0.15s;
}

.history-card:hover {
  border-color: var(--td-brand-color);
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.card-env {
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  color: var(--td-brand-color);
  background: var(--td-brand-color-light);
  padding: 2px 6px;
  border-radius: var(--td-radius-small);
}

.card-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 8px;
  text-transform: uppercase;
}

.status-success { background: var(--td-success-color-1); color: var(--td-success-color); }
.status-error { background: var(--td-error-color-1); color: var(--td-error-color); }
.status-muted { background: var(--td-bg-color-container-hover); color: var(--td-text-color-secondary); }

.card-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--td-text-color-placeholder);
}

.card-body {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: var(--td-font-size-body-small);
}

.card-user {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--td-text-color-secondary);
}

.card-changes {
  display: flex;
  align-items: center;
  gap: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}

.change-added { color: var(--td-success-color); }
.change-removed { color: var(--td-error-color); }
.change-direct { color: var(--td-brand-color); }

.card-duration {
  margin-left: auto;
  font-size: 11px;
  color: var(--td-text-color-placeholder);
}

.card-error {
  margin-top: 6px;
  font-size: 11px;
  color: var(--td-error-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
