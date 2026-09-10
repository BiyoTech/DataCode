<script setup lang="ts">
/**
 * ErrorCard — displays error/warning messages in the chat timeline.
 * Used for: session creation failures, agent errors, model request errors, etc.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()

const props = defineProps<{
  entry: TimelineEntry
}>()

const isError = computed(() => props.entry.type === 'system.error')
const icon = computed(() => isError.value ? '✗' : '⚠')
const message = computed(() => props.entry.payload.message || t('common.error'))
const detail = computed(() => props.entry.payload.detail || '')
const showDetail = ref(false)

function toggleDetail() {
  showDetail.value = !showDetail.value
}
</script>

<template>
  <div class="error-card" :class="{ 'is-warning': !isError, 'is-error': isError }">
    <div class="error-header">
      <span class="error-icon">{{ icon }}</span>
      <!-- The message is plain selectable text: an error is the thing users most
           often need to copy (into a search, a bug report, or this very chat).
           The disclosure affordance is therefore a button of its own rather than
           a click handler on the whole row — a row-wide handler forces
           user-select: none, and without it a drag to select text toggles the
           detail panel instead. -->
      <span class="error-message">{{ message }}</span>
      <button
        v-if="detail"
        type="button"
        class="error-toggle"
        :aria-expanded="showDetail"
        :title="t('common.detail')"
        @click="toggleDetail"
      >{{ showDetail ? '▾' : '▸' }}</button>
    </div>
    <div v-if="showDetail && detail" class="error-detail">
      <pre>{{ detail }}</pre>
    </div>
  </div>
</template>

<style scoped>
.error-card {
  border-radius: 8px;
  padding: 10px 14px;
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.5;
  max-width: 90%;
  align-self: flex-start;
}

.is-error {
  background: var(--error-subtle);
  border: 1px solid var(--error);
  color: var(--error);
}

.is-warning {
  background: var(--warning-subtle);
  border: 1px solid var(--warning);
  color: var(--warning);
}

.error-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.error-icon {
  font-size: 14px;
  font-weight: 700;
  flex-shrink: 0;
  /* Excluded from selection so a copied error reads as the message alone,
     without the decorative glyph in front of it. */
  user-select: none;
}

.error-message {
  flex: 1;
  word-break: break-word;
  user-select: text;
  cursor: text;
}

.error-toggle {
  flex-shrink: 0;
  padding: 0 2px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.error-detail {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--bg-primary);
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  overflow-x: auto;
}

.error-detail pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}
</style>
