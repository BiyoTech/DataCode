<script setup lang="ts">
/**
 * GitAuthPanel - Shown in the bottom panel when no Git credentials are configured.
 * Guides the user to the Settings page to configure Git credentials.
 * Does NOT provide inline credential forms in the bottom panel.
 */
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const emit = defineEmits<{
  (e: 'goToSettings'): void
}>()
</script>

<template>
  <div class="auth-panel">
    <div class="auth-content">
      <div class="auth-icon">⎇</div>
      <h3 class="auth-title">{{ t('git.authTitle') }}</h3>
      <p class="auth-desc">{{ t('git.authDescription') }}</p>
      <button class="btn-settings" @click="emit('goToSettings')">
        {{ t('settings.gitCredentials') }}
        <span class="arrow">→</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.auth-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  width: 100%;
  padding: 24px;
}

.auth-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  max-width: 320px;
}

.auth-icon {
  font-size: 32px;
  margin-bottom: 12px;
  opacity: 0.6;
}

.auth-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.auth-desc {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  margin-bottom: 16px;
}

.btn-settings {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.15s;
}

.btn-settings:hover {
  filter: brightness(1.1);
}

.arrow {
  font-size: 14px;
}
</style>
