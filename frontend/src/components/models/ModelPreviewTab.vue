<script setup lang="ts">
/**
 * ModelPreviewTab — executes model evaluation and shows a data preview table.
 * Calls POST /api/v1/sqlmesh/evaluate with the model name.
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useModelsStore } from '@/stores/models'
import { useWorkspaceStore } from '@/stores/workspace'
import api from '@/utils/api'

const { t } = useI18n()
const modelsStore = useModelsStore()
const workspaceStore = useWorkspaceStore()

const columns = ref<string[]>([])
const rows = ref<any[][]>([])
const totalRows = ref(0)
const loading = ref(false)
const error = ref('')
const limit = ref(100)

async function loadPreview() {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !modelsStore.selectedModelName) return

  loading.value = true
  error.value = ''
  try {
    const { data } = await api.post('/sqlmesh/evaluate', {
      workspace_path: ws.path,
      // Backend EvaluateRequest expects `model`, not `model_name`.
      model: modelsStore.selectedModelName,
      // Carry the model's gateway so external models bound to a non-default
      // gateway resolve against the correct SQLMesh context.
      gateway: modelsStore.selectedModel?.gateway || undefined,
      limit: limit.value,
    })
    columns.value = data.columns || []
    rows.value = data.rows || []
    totalRows.value = data.total_rows || rows.value.length
  } catch (e: any) {
    error.value = e?.response?.data?.error || e?.message || 'Failed to load preview'
    columns.value = []
    rows.value = []
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="preview-tab">
    <!-- Action bar -->
    <div class="preview-actions">
      <t-button theme="primary" size="small" :loading="loading" @click="loadPreview">
        <template #icon><t-icon name="play" /></template>
        {{ t('models.runPreview') }}
      </t-button>
      <span v-if="totalRows > 0" class="row-count">
        {{ totalRows }} {{ t('models.rows') }}
      </span>
    </div>

    <!-- Error -->
    <div v-if="error" class="preview-error">
      <t-icon name="error-circle" size="14px" />
      {{ error }}
    </div>

    <!-- Loading -->
    <div v-if="loading" class="preview-loading">
      <t-icon name="loading" size="20px" class="spinning" />
    </div>

    <!-- Data table -->
    <div v-else-if="columns.length > 0" class="preview-table-wrap">
      <table class="preview-table">
        <thead>
          <tr>
            <th v-for="col in columns" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rIdx) in rows" :key="rIdx">
            <td v-for="(cell, cIdx) in row" :key="cIdx">{{ cell ?? 'NULL' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Empty -->
    <div v-else-if="!loading && !error" class="preview-empty">
      <p>{{ t('models.previewHint') }}</p>
    </div>
  </div>
</template>

<style scoped>
.preview-tab {
  padding: 20px 24px;
}

.preview-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.row-count {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
}

.preview-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--td-error-color-1);
  border: 1px solid var(--td-error-color);
  border-radius: var(--td-radius-medium);
  color: var(--td-error-color);
  font-size: var(--td-font-size-body-small);
  margin-bottom: 12px;
}

.preview-loading {
  display: flex;
  justify-content: center;
  padding: 40px;
}

.spinning {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.preview-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
}

.preview-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--td-font-size-body-small);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.preview-table th {
  background: var(--td-bg-color-secondarycontainer);
  font-weight: 600;
  color: var(--td-text-color-primary);
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--td-component-stroke);
  white-space: nowrap;
}

.preview-table td {
  padding: 6px 12px;
  border-bottom: 1px solid var(--td-component-stroke);
  color: var(--td-text-color-primary);
  white-space: nowrap;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preview-table tr:hover td {
  background: var(--td-bg-color-container-hover);
}

.preview-empty {
  text-align: center;
  padding: 40px;
  color: var(--td-text-color-placeholder);
  font-size: var(--td-font-size-body-medium);
}
</style>
