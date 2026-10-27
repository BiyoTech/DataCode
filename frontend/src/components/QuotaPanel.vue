/**
 * QuotaPanel - the caller's own tenant budget and usage (GET /api/v1/quotas).
 *
 * The read half of quota enforcement: a user rejected with 429 lands here to
 * see which budget ran out and by how much. Lives under Settings → Organization
 * because quotas are tenant-scoped, not per-user.
 *
 * Rendering rules that matter:
 * - `current === null` renders "unknown", never 0 — the backend fails open when
 *   it cannot count, and claiming a full budget at that moment would be a lie.
 * - An exhausted resource is highlighted so the panel answers "why was I
 *   rejected?" without the user having to compare numbers themselves.
 */
<template>
  <div class="quota-panel">
    <div v-if="quotaStore.loading && !quotaStore.overview" class="quota-hint">
      {{ t('quotas.loading') }}
    </div>

    <div v-else-if="quotaStore.error" class="quota-hint quota-hint-error">
      {{ t('quotas.loadFailed') }}
      <a class="quota-retry" @click.prevent="quotaStore.refresh()" href="#">
        {{ t('quotas.retry') }}
      </a>
    </div>

    <template v-else-if="quotaStore.overview">
      <div
        v-for="item in quotaStore.overview.resources"
        :key="item.resource"
        class="quota-row"
        :class="{ 'quota-row-exhausted': item.exhausted }"
      >
        <div class="quota-label">
          <span>{{ t(resourceKey(item.resource)) }}</span>
          <span v-if="item.exhausted" class="quota-badge">
            {{ t('quotas.exhausted') }}
          </span>
        </div>
        <div class="quota-value">
          <template v-if="item.current === null">
            <span class="quota-unknown">{{ t('quotas.unknown') }}</span>
            <span class="quota-limit">/ {{ item.limit }}</span>
          </template>
          <template v-else>
            <span class="quota-current" :class="{ 'quota-current-exhausted': item.exhausted }">
              {{ item.current }}
            </span>
            <span class="quota-limit">/ {{ item.limit }}</span>
          </template>
        </div>
        <div class="quota-bar">
          <div
            class="quota-bar-fill"
            :class="{ 'quota-bar-fill-exhausted': item.exhausted }"
            :style="{ width: barWidth(item) }"
          />
        </div>
      </div>

      <p v-if="peakNote" class="quota-note">{{ peakNote }}</p>
      <p class="quota-note">{{ t('quotas.contactAdmin') }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuotaStore } from '@/stores/quotas'

const { t } = useI18n()
const quotaStore = useQuotaStore()

onMounted(() => {
  // Fresh on every mount: quota is live state, and a stale panel directly
  // contradicts the 429 that sent the user here.
  void quotaStore.refresh()
})

/** Resource codes the backend sends, mapped to i18n keys. Unknown codes fall
 * back to the raw code so a new backend resource still renders something. */
function resourceKey(resource: string): string {
  const known: Record<string, string> = {
    workspace: 'quotas.workspace',
    environment: 'quotas.environment',
    concurrent_leases: 'quotas.concurrent_leases',
    runtime_replicas: 'quotas.runtime_replicas',
  }
  return known[resource] || resource
}

function barWidth(item: { current: number | null; limit: number }): string {
  if (item.current === null || item.limit <= 0) return '0%'
  return `${Math.min(100, Math.round((item.current / item.limit) * 100))}%`
}

const peakNote = computed(() => {
  const peak = quotaStore.overview?.peakConcurrentLeases ?? 0
  return peak > 0 ? t('quotas.peakLeases', { peak }) : ''
})
</script>

<style scoped>
.quota-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.quota-hint {
  color: var(--td-text-color-secondary);
  font-size: 13px;
}

.quota-hint-error {
  color: var(--td-error-color);
}

.quota-retry {
  margin-left: 8px;
  cursor: pointer;
}

.quota-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) auto;
  grid-template-areas: 'label value' 'bar bar';
  row-gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--td-component-border);
  border-radius: 6px;
}

.quota-row-exhausted {
  border-color: var(--td-error-color);
}

.quota-label {
  grid-area: label;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--td-text-color-primary);
}

.quota-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--td-error-color);
  color: #fff;
}

.quota-value {
  grid-area: value;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.quota-current {
  color: var(--td-text-color-primary);
  font-weight: 600;
}

.quota-current-exhausted {
  color: var(--td-error-color);
}

.quota-unknown {
  color: var(--td-text-color-placeholder);
  font-style: italic;
}

.quota-limit {
  color: var(--td-text-color-secondary);
  margin-left: 4px;
}

.quota-bar {
  grid-area: bar;
  height: 4px;
  border-radius: 2px;
  background: var(--td-bg-color-component);
  overflow: hidden;
}

.quota-bar-fill {
  height: 100%;
  background: var(--td-brand-color);
  transition: width 0.3s;
}

.quota-bar-fill-exhausted {
  background: var(--td-error-color);
}

.quota-note {
  margin: 0;
  font-size: 12px;
  color: var(--td-text-color-placeholder);
}
</style>
