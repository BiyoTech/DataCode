<script setup lang="ts">
/**
 * BottomPanel - IntelliJ-style bottom tool panel.
 * Contains Git (source control) and Log tabs.
 * Positioned at the bottom of the main content area.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import GitPanel from '@/components/git/GitPanel.vue'
import LogPanel from '@/components/logs/LogPanel.vue'
import { usePermission } from '@/composables/usePermission'

const { t } = useI18n()
const { canInWorkspace } = usePermission()

// Logs can quote SQL, data values and tool output, so they carry their own
// permission rather than riding on workspace:code:read.
const canReadLogs = computed(() => canInWorkspace('workspace:logs:read'))

defineProps<{
  activeTab: 'git' | 'log'
}>()

defineEmits<{
  (e: 'change-tab', tab: 'git' | 'log'): void
  (e: 'close'): void
  (e: 'goToSettings'): void
}>()
</script>

<template>
  <div class="bottom-panel">
    <!-- Tab bar -->
    <div class="bottom-tabs">
      <div class="tab-group">
        <button
          :class="['tab-btn', { active: activeTab === 'git' }]"
          @click="$emit('change-tab', 'git')"
        >
          <t-icon name="git-branch" size="14px" class="tab-icon" />
          <span>Git</span>
        </button>
        <button
          :class="['tab-btn', { active: activeTab === 'log' }]"
          @click="$emit('change-tab', 'log')"
        >
          <t-icon name="history" size="14px" class="tab-icon" />
          <span>Log</span>
        </button>
      </div>
      <div class="tab-actions">
        <button class="close-btn" @click="$emit('close')" title="Close"><t-icon name="close" size="12px" /></button>
      </div>
    </div>

    <!-- Panel content -->
    <div class="bottom-content">
      <!-- Git tab -->
      <template v-if="activeTab === 'git'">
        <GitPanel @go-to-settings="$emit('goToSettings')" />
      </template>

      <!-- Log tab -->
      <template v-if="activeTab === 'log'">
        <LogPanel v-if="canReadLogs" />
        <div v-else class="empty-state">
          {{ t('logs.forbidden') }}
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.bottom-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.bottom-tabs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 32px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  padding: 0 8px;
  flex-shrink: 0;
}

.tab-group {
  display: flex;
  align-items: center;
  gap: 2px;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 4px 4px 0 0;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.1s, border-color 0.1s;
}

.tab-btn:hover {
  color: var(--text-primary);
}

.tab-btn.active {
  color: var(--text-primary);
  border-bottom-color: var(--accent);
}

.tab-icon {
  color: var(--text-secondary);
}

.tab-actions {
  display: flex;
  align-items: center;
}

.close-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
}

.close-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.bottom-content {
  flex: 1;
  overflow: hidden;
}

.empty-state {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}
</style>
