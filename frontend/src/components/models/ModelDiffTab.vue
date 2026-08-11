<script setup lang="ts">
/**
 * ModelDiffTab — shows the SQL diff for the selected model between current and latest plan.
 * Calls POST /api/v1/sqlmesh/render to get the rendered SQL, then compares with stored version.
 * Also references the ModelsDiff logic from the sqlmesh source (direct/indirect/metadata changes).
 */
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useModelsStore } from '@/stores/models'
import { useWorkspaceStore } from '@/stores/workspace'
import api from '@/utils/api'

const { t } = useI18n()
const modelsStore = useModelsStore()
const workspaceStore = useWorkspaceStore()

const renderedSql = ref('')
const loading = ref(false)
const error = ref('')

onMounted(() => {
  if (modelsStore.selectedModelName) {
    fetchRenderedSql()
  }
})

async function fetchRenderedSql() {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !modelsStore.selectedModelName) return

  loading.value = true
  error.value = ''
  try {
    const { data } = await api.post('/sqlmesh/render', {
      workspace_path: ws.path,
      model_name: modelsStore.selectedModelName,
      // Carry the model's gateway so external models bound to a non-default
      // gateway resolve against the correct SQLMesh context.
      gateway: modelsStore.selectedModel?.gateway || undefined,
    })
    renderedSql.value = data.sql || ''
  } catch (e: any) {
    error.value = e?.response?.data?.error || e?.message || 'Failed to render SQL'
  } finally {
    loading.value = false
  }
}

// Show the original SQL (from model detail) vs rendered SQL for comparison
const originalSql = ref('')
if (modelsStore.modelDetail?.sql) {
  originalSql.value = modelsStore.modelDetail.sql
}
</script>

<template>
  <div class="diff-tab">
    <!-- Loading -->
    <div v-if="loading" class="tab-loading">
      <t-icon name="loading" size="20px" class="spinning" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="diff-error">
      <t-icon name="error-circle" size="14px" />
      {{ error }}
    </div>

    <template v-else>
      <!-- Source SQL -->
      <div class="diff-section">
        <div class="diff-header">
          <t-icon name="file-code" size="14px" />
          <span>{{ t('models.sourceSql') }}</span>
        </div>
        <pre class="diff-code">{{ modelsStore.modelDetail?.sql || t('models.noSql') }}</pre>
      </div>

      <!-- Rendered SQL (macros expanded) -->
      <div class="diff-section">
        <div class="diff-header">
          <t-icon name="view-module" size="14px" />
          <span>{{ t('models.renderedSql') }}</span>
          <t-button size="small" variant="text" @click="fetchRenderedSql">
            <template #icon><t-icon name="refresh" /></template>
          </t-button>
        </div>
        <pre class="diff-code">{{ renderedSql || t('models.clickRender') }}</pre>
      </div>
    </template>
  </div>
</template>

<style scoped>
.diff-tab {
  padding: 20px 24px;
}

.tab-loading {
  display: flex;
  justify-content: center;
  padding: 40px;
}

.spinning { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.diff-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--td-error-color-1);
  border: 1px solid var(--td-error-color);
  border-radius: var(--td-radius-medium);
  color: var(--td-error-color);
  font-size: var(--td-font-size-body-small);
}

.diff-section {
  margin-bottom: 20px;
}

.diff-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--td-font-size-body-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  margin-bottom: 8px;
}

.diff-code {
  background: var(--td-bg-color-secondarycontainer);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 14px 16px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--td-text-color-primary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 400px;
  overflow-y: auto;
  margin: 0;
}
</style>
