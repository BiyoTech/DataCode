<script setup lang="ts">
import type { TimelineEntry } from '@/types/agent'

defineProps<{
  entry: TimelineEntry
}>()
</script>

<template>
  <div class="handoff-chip">
    <span class="handoff-from">{{ entry.payload.from_agent }}</span>
    <span class="handoff-arrow">→</span>
    <span class="handoff-to">{{ entry.payload.to_agent }}</span>
    <span v-if="entry.payload.reason" class="handoff-reason">{{ entry.payload.reason }}</span>
  </div>
</template>

<style scoped>
.handoff-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  align-self: center;
  padding: 6px 14px;
  border-radius: 20px;
  background: var(--accent-subtle);
  border: 1px solid var(--border);
  font-size: 12px;
}

.handoff-from {
  color: var(--text-secondary);
}

.handoff-arrow {
  color: var(--accent);
  font-weight: bold;
}

.handoff-to {
  color: var(--text-primary);
  font-weight: 500;
}

.handoff-reason {
  color: var(--text-secondary);
  font-style: italic;
  margin-left: 4px;
}
</style>
