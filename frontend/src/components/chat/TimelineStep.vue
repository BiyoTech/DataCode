<script setup lang="ts">
/**
 * TimelineStep — renders non-tool, non-message agent timeline steps:
 *   - thought.plan / thought.update : reasoning steps (🧠)
 *   - artifact.created / artifact.diff : generated files (📦)
 *   - agent.finish : task completion marker (✓)
 *
 * Each step is a compact line with an icon, a title, and an optional sub-line.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()

const props = defineProps<{
  entry: TimelineEntry
}>()

const type = computed(() => props.entry.type)
const payload = computed(() => props.entry.payload || {})

const icon = computed(() => {
  switch (type.value) {
    case 'thought.plan':
    case 'thought.update':
      return '🧠'
    case 'artifact.created':
    case 'artifact.diff':
      return '📦'
    case 'artifact.image':
      return '🖼'
    case 'artifact.chart':
      return '📊'
    case 'task.created':
      return '•'
    case 'task.running':
      return '▶'
    case 'task.completed':
      return '✓'
    case 'task.failed':
      return '✗'
    case 'agent.finish':
      return payload.value.outcome === 'partial' ? '⚠' : '✓'
    default:
      return '•'
  }
})

// Primary title line per step type.
const title = computed(() => {
  switch (type.value) {
    case 'thought.plan':
      return t('chat.timeline.planning')
    case 'thought.update':
      return payload.value.thought || t('chat.timeline.thinking')
    case 'artifact.created':
    case 'artifact.diff':
      return t('chat.timeline.artifact')
    case 'artifact.image':
      return t('chat.timeline.artifactImage')
    case 'artifact.chart':
      return t('chat.timeline.artifactChart')
    case 'task.created':
      return t('chat.timeline.taskCreated')
    case 'task.running':
      return t('chat.timeline.taskRunning')
    case 'task.completed':
      return t('chat.timeline.taskCompleted')
    case 'task.failed':
      return t('chat.timeline.taskFailed')
    case 'agent.finish':
      if (payload.value.outcome === 'partial') {
        return payload.value.summary || t('chat.timeline.partialDone')
      }
      return payload.value.summary || t('chat.timeline.done')
    default:
      return ''
  }
})

// Optional secondary line (plan items joined, artifact path, task title, etc.).
const subtitle = computed(() => {
  switch (type.value) {
    case 'thought.plan':
      return Array.isArray(payload.value.plan) ? payload.value.plan.join(' · ') : ''
    case 'artifact.created':
    case 'artifact.diff':
      return payload.value.file_path || ''
    case 'artifact.image':
    case 'artifact.chart':
      return payload.value.title || payload.value.artifact_id || ''
    case 'task.created':
    case 'task.running':
    case 'task.completed':
    case 'task.failed':
      return payload.value.title || payload.value.task_id || ''
    default:
      return ''
  }
})

const isDone = computed(
  () =>
    type.value === 'agent.finish' ||
    type.value === 'task.completed',
)
const isPartial = computed(
  () =>
    type.value === 'agent.finish' && payload.value.outcome === 'partial',
)
const isFailed = computed(() => type.value === 'task.failed')
</script>

<template>
  <div class="timeline-step" :class="{ done: isDone, partial: isPartial, failed: isFailed }">
    <span class="step-icon">{{ icon }}</span>
    <div class="step-body">
      <div class="step-title">{{ title }}</div>
      <div v-if="subtitle" class="step-subtitle">{{ subtitle }}</div>
    </div>
  </div>
</template>

<style scoped>
.timeline-step {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  align-self: flex-start;
  max-width: 90%;
  font-size: 13px;
}

.step-icon {
  font-size: 15px;
  flex-shrink: 0;
  line-height: 1.4;
}

.step-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.step-title {
  color: var(--text-primary);
  font-weight: 500;
}

.timeline-step.done .step-title {
  color: var(--success);
}

.timeline-step.partial .step-title {
  color: var(--warning, #d97706);
}

.timeline-step.failed .step-title {
  color: var(--error, #dc2626);
}

.step-subtitle {
  color: var(--text-secondary);
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  word-break: break-word;
}
</style>
