<script setup lang="ts">
/**
 * AiContextBlock — renders an AI context (instructions / synonyms / examples)
 * in a compact, consistent card. Shared by the overview and metrics tabs so
 * AI guidance is displayed identically everywhere.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { AIContextValue } from '@/types/ontology'
import { normaliseAiContext } from '@/utils/ontology'

const { t } = useI18n()

const props = defineProps<{
  context: AIContextValue | null | undefined
  // When true, render without the outer card chrome (for nested/compact use).
  compact?: boolean
}>()

const ctx = computed(() => normaliseAiContext(props.context))
</script>

<template>
  <div v-if="ctx" class="ai-context" :class="{ compact }">
    <div class="ai-header">
      <t-icon name="chat-bubble" size="14px" />
      <span>{{ t('models.ontology.aiContext') }}</span>
    </div>

    <p v-if="ctx.instructions" class="ai-instructions">{{ ctx.instructions }}</p>

    <div v-if="ctx.synonyms.length" class="ai-row">
      <span class="ai-label">{{ t('models.ontology.synonyms') }}</span>
      <span class="ai-tags">
        <t-tag v-for="s in ctx.synonyms" :key="s" size="small" variant="light" theme="primary">
          {{ s }}
        </t-tag>
      </span>
    </div>

    <div v-if="ctx.examples.length" class="ai-row">
      <span class="ai-label">{{ t('models.ontology.examples') }}</span>
      <ul class="ai-examples">
        <li v-for="(ex, i) in ctx.examples" :key="i">{{ ex }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.ai-context {
  border: 1px solid var(--td-component-stroke);
  border-left: 3px solid var(--td-brand-color);
  border-radius: var(--td-radius-medium);
  background: var(--td-bg-color-secondarycontainer);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ai-context.compact {
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-left: 2px solid var(--td-brand-color-light);
}

.ai-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--td-font-size-body-small);
  font-weight: 600;
  color: var(--td-brand-color);
}

.ai-instructions {
  margin: 0;
  font-size: var(--td-font-size-body-small);
  line-height: 1.6;
  color: var(--td-text-color-primary);
  white-space: pre-wrap;
}

.ai-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ai-label {
  font-size: 11px;
  font-weight: 500;
  color: var(--td-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.ai-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.ai-examples {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ai-examples li {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-primary);
  line-height: 1.5;
}
</style>
