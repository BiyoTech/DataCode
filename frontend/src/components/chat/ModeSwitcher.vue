<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useChatStore } from '@/stores/chat'
import type { SessionMode } from '@/types/agent'

const { t } = useI18n()
const chatStore = useChatStore()

const modes: { id: SessionMode; icon: string }[] = [
  { id: 'ask', icon: '💬' },
  { id: 'craft', icon: '🛠' },
  { id: 'design', icon: '📋' },
]

function selectMode(mode: SessionMode) {
  // Optional on the store contract: only created with enableModeSwitch.
  void chatStore.switchMode?.(mode)
}
</script>

<template>
  <div class="mode-switcher">
    <button
      v-for="mode in modes"
      :key="mode.id"
      :class="['mode-tab', { active: chatStore.currentMode === mode.id }]"
      @click="selectMode(mode.id)"
    >
      <span class="mode-icon">{{ mode.icon }}</span>
      <span class="mode-label">{{ t(`chat.modes.${mode.id}`) }}</span>
    </button>
  </div>
</template>

<style scoped>
.mode-switcher {
  display: flex;
  gap: 4px;
  padding: 4px;
  background: var(--bg-primary);
  border-radius: 10px;
  border: 1px solid var(--border);
}

.mode-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.mode-tab:hover {
  color: var(--text-primary);
  background: var(--accent-subtler);
}

.mode-tab.active {
  background: var(--accent-subtle);
  color: var(--accent);
  box-shadow: 0 0 0 1px var(--border);
}

.mode-icon {
  font-size: 14px;
}

.mode-label {
  font-size: 12px;
}
</style>
