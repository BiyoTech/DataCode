<script setup lang="ts">
/**
 * ModelOverviewTab — displays model description, owner, tags, and column schema table.
 * Matches the reference UI: description row + owner row + columns table (index, name, type, description, desensitization, tags).
 */
import { useI18n } from 'vue-i18n'
import { useModelsStore } from '@/stores/models'

const { t } = useI18n()
const modelsStore = useModelsStore()
</script>

<template>
  <div class="overview-tab">
    <!-- Loading -->
    <div v-if="modelsStore.detailLoading" class="tab-loading">
      <t-icon name="loading" size="20px" class="spinning" />
    </div>

    <template v-else-if="modelsStore.modelDetail">
      <!-- Meta section -->
      <div class="meta-section">
        <div class="meta-row">
          <span class="meta-label">{{ t('models.description') }}</span>
          <span class="meta-value">{{ modelsStore.modelDetail.description || '–' }}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('models.owner') }}</span>
          <span class="meta-value">{{ modelsStore.modelDetail.details?.owner || '–' }}</span>
        </div>
        <div v-if="modelsStore.modelDetail.details?.tags" class="meta-row">
          <span class="meta-label">{{ t('models.tags') }}</span>
          <span class="meta-value">
            <t-tag
              v-for="tag in modelsStore.modelDetail.details.tags.split(',')"
              :key="tag.trim()"
              size="small"
              variant="light"
            >{{ tag.trim() }}</t-tag>
          </span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('models.kind') }}</span>
          <span class="meta-value">{{ modelsStore.modelDetail.details?.kind || '–' }}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('models.dialect') }}</span>
          <span class="meta-value">{{ modelsStore.modelDetail.dialect || '–' }}</span>
        </div>
        <div v-if="modelsStore.modelDetail.gateway" class="meta-row">
          <span class="meta-label">{{ t('models.gateway') }}</span>
          <span class="meta-value mono">{{ modelsStore.modelDetail.gateway }}</span>
        </div>
        <div v-if="modelsStore.modelDetail.details?.cron" class="meta-row">
          <span class="meta-label">{{ t('models.cron') }}</span>
          <span class="meta-value mono">{{ modelsStore.modelDetail.details.cron }}</span>
        </div>
      </div>

      <!-- Columns table -->
      <div class="columns-section">
        <h3 class="section-title">{{ t('models.columns') }}</h3>
        <table class="columns-table">
          <thead>
            <tr>
              <th class="col-idx">#</th>
              <th class="col-name">{{ t('models.columnName') }}</th>
              <th class="col-type">{{ t('models.columnType') }}</th>
              <th class="col-desc">{{ t('models.columnDesc') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(col, idx) in modelsStore.modelDetail.columns" :key="col.name">
              <td class="col-idx">{{ idx + 1 }}</td>
              <td class="col-name mono">{{ col.name }}</td>
              <td class="col-type mono">{{ col.type }}</td>
              <td class="col-desc">{{ col.description || '–' }}</td>
            </tr>
            <tr v-if="modelsStore.modelDetail.columns.length === 0">
              <td colspan="4" class="empty-row">{{ t('models.noColumns') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.overview-tab {
  padding: 20px 24px;
}

.tab-loading {
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

/* ─── Meta ─── */
.meta-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 24px;
}

.meta-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.meta-label {
  min-width: 80px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
  font-weight: 500;
}

.meta-value {
  font-size: var(--td-font-size-body-medium);
  color: var(--td-text-color-primary);
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.mono {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 13px;
}

/* ─── Columns table ─── */
.section-title {
  font-size: var(--td-font-size-body-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  margin: 0 0 12px;
}

.columns-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--td-font-size-body-small);
}

.columns-table th {
  background: var(--td-bg-color-secondarycontainer);
  font-weight: 600;
  color: var(--td-text-color-primary);
  text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.columns-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--td-component-stroke);
  color: var(--td-text-color-primary);
  vertical-align: top;
}

.columns-table tr:hover td {
  background: var(--td-bg-color-container-hover);
}

.col-idx {
  width: 50px;
  color: var(--td-text-color-secondary);
}

.col-name {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-weight: 500;
}

.col-type {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  color: var(--td-text-color-secondary);
}

.empty-row {
  text-align: center;
  color: var(--td-text-color-placeholder);
  padding: 20px;
}
</style>
