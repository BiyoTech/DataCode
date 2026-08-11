<script setup lang="ts">
/**
 * SystemNoticeCard — structured display for system.warning events
 * (auto-resume, todo continuation, turn limits, etc.).
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TimelineEntry } from '@/types/agent'

const { t, te } = useI18n()

const props = defineProps<{
  entry: TimelineEntry
}>()

const code = computed(() => String(props.entry.payload?.code || ''))

const tone = computed(() => {
  switch (code.value) {
    case 'AUTO_RESUME':
    case 'TODO_CONTINUATION':
      return 'info'
    case 'MAX_TURNS_EXCEEDED':
    case 'TURN_BUDGET_EXHAUSTED':
      return 'warning'
    default:
      return 'warning'
  }
})

const icon = computed(() => {
  switch (tone.value) {
    case 'info':
      return '⟳'
    case 'warning':
      return '⚠'
    default:
      return '⚠'
  }
})

const title = computed(() => {
  const key = `chat.systemNotice.${code.value}.title`
  if (code.value && te(key)) return t(key)
  return t('chat.systemNotice.default.title')
})

const message = computed(() => {
  const payloadMsg = props.entry.payload?.message
  if (payloadMsg) return payloadMsg
  const key = `chat.systemNotice.${code.value}.message`
  if (code.value && te(key)) return t(key)
  return t('chat.systemNotice.default.message')
})

const suggestion = computed(() => {
  const key = `chat.systemNotice.${code.value}.suggestion`
  if (code.value && te(key)) return t(key)
  return props.entry.payload?.suggestion || ''
})
</script>

<template>
  <div class="system-notice" :class="`tone-${tone}`">
    <div class="notice-header">
      <span class="notice-icon" aria-hidden="true">{{ icon }}</span>
      <div class="notice-body">
        <div v-if="code" class="notice-code">{{ code }}</div>
        <div class="notice-message">{{ message }}</div>
        <div v-if="suggestion" class="notice-suggestion">{{ suggestion }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.system-notice {
  border-radius: 8px;
  padding: 10px 14px;
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.5;
  max-width: 92%;
  align-self: flex-start;
}

.tone-info {
  background: color-mix(in srgb, var(--td-brand-color) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--td-brand-color) 35%, transparent);
  color: var(--td-text-color-primary);
}

.tone-warning {
  background: var(--warning-subtle, color-mix(in srgb, #f5a623 12%, transparent));
  border: 1px solid var(--warning, #e6a23c);
  color: var(--td-text-color-primary);
}

.notice-header {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.notice-icon {
  font-size: 15px;
  line-height: 1.4;
  flex-shrink: 0;
}

.notice-body {
  flex: 1;
  min-width: 0;
}

.notice-code {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--td-text-color-secondary);
  margin-bottom: 2px;
}

.notice-message {
  word-break: break-word;
}

.notice-suggestion {
  margin-top: 6px;
  font-size: 12px;
  color: var(--td-text-color-secondary);
}
</style>
