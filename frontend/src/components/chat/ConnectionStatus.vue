<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useChatStore } from '@/stores/chat'

const { t } = useI18n()
const chatStore = useChatStore()

const dotColor = computed(() => {
  switch (chatStore.connectionState) {
    case 'connected': return 'var(--success)'
    case 'connecting':
    case 'reconnecting': return 'var(--warning)'
    case 'disconnected': return 'var(--error)'
    default: return 'var(--text-muted)'
  }
})

const statusText = computed(() => {
  return t(`chat.connection.${chatStore.connectionState}`)
})
</script>

<template>
  <div class="connection-status" :title="statusText">
    <span class="status-dot" :style="{ backgroundColor: dotColor }"></span>
    <span class="status-text">{{ statusText }}</span>
  </div>
</template>

<style scoped>
.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 12px;
  background: var(--bg-primary);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  animation: pulse-dot 2s infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.status-text {
  font-size: 11px;
  color: var(--text-secondary);
}
</style>
