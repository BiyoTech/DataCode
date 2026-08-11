<script setup lang="ts">
/**
 * AbiPlaceholder — shared empty-state scaffold for A-BI sections that are not
 * yet implemented. Renders a section header plus a centered placeholder with a
 * "coming soon" hint, so each section has a consistent skeleton.
 */
import { useI18n } from 'vue-i18n'

defineProps<{
  // TDesign icon name shown in the placeholder.
  icon: string
  // Section title text (already translated by the caller).
  title: string
  // Optional short description under the title.
  description?: string
}>()

const { t } = useI18n()
</script>

<template>
  <div class="abi-section">
    <header class="section-header">
      <h2 class="section-title">{{ title }}</h2>
    </header>

    <div class="section-placeholder">
      <t-icon :name="icon" size="48px" />
      <p class="placeholder-desc">{{ description || t('abi.common.comingSoon') }}</p>
    </div>
  </div>
</template>

<style scoped>
.abi-section {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.section-header {
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
}

.section-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.section-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-muted);
}

.placeholder-desc {
  margin: 0;
  font-size: 13px;
}
</style>
