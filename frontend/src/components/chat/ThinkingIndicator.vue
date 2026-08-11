<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()

defineProps<{
  /** Optional thought.plan entry to show plan steps */
  planEntry?: TimelineEntry
}>()
</script>

<template>
  <div class="thinking-indicator">
    <div v-if="planEntry && planEntry.payload.plan?.length" class="thinking-plan">
      <div class="plan-header">{{ t('chat.thinking.planning') }}</div>
      <ul class="plan-steps">
        <li
          v-for="(step, idx) in planEntry.payload.plan"
          :key="idx"
          class="plan-step"
        >
          {{ step }}
        </li>
      </ul>
    </div>
    <div v-else class="thinking-dots">
      <span class="dot"></span>
      <span class="dot"></span>
      <span class="dot"></span>
      <span class="thinking-text">{{ t('chat.thinking.label') }}</span>
    </div>
  </div>
</template>

<style scoped>
.thinking-indicator {
  align-self: flex-start;
  max-width: 80%;
}

.thinking-dots {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 16px 16px 16px 4px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  animation: bounce 1.4s infinite ease-in-out;
}

.dot:nth-child(2) {
  animation-delay: 0.2s;
}

.dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes bounce {
  0%, 80%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  40% {
    transform: translateY(-6px);
    opacity: 1;
  }
}

.thinking-text {
  font-size: 13px;
  color: var(--text-secondary);
  margin-left: 4px;
}

.thinking-plan {
  padding: 12px 16px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
}

.plan-header {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.plan-steps {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.plan-step {
  font-size: 13px;
  color: var(--text-primary);
  padding-left: 16px;
  position: relative;
}

.plan-step::before {
  content: '•';
  position: absolute;
  left: 4px;
  color: var(--accent);
}
</style>
