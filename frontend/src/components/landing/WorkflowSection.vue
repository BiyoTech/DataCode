<script setup lang="ts">
/**
 * WorkflowSection — four-step journey (Describe → Generate → Review → Ship)
 * rendered as a numbered horizontal track with connecting lines.
 */
import { useI18n } from 'vue-i18n'

import { vReveal } from '@/composables/useReveal'

const { t } = useI18n()

/** Ordered step keys; display numbers are derived from the index. */
const steps = ['describe', 'generate', 'review', 'ship'] as const
</script>

<template>
  <section id="workflow" class="section">
    <div class="section-inner">
      <div class="section-head" v-reveal>
        <h2 class="section-title">{{ t('landing.workflow.title') }}</h2>
        <p class="section-subtitle">{{ t('landing.workflow.subtitle') }}</p>
      </div>

      <ol class="steps-track">
        <li
          v-for="(key, index) in steps"
          :key="key"
          class="step"
          v-reveal="index * 90"
        >
          <span class="step-number">{{ String(index + 1).padStart(2, '0') }}</span>
          <h3 class="step-title">{{ t(`landing.workflow.steps.${key}.title`) }}</h3>
          <p class="step-desc">{{ t(`landing.workflow.steps.${key}.desc`) }}</p>
          <span v-if="index < steps.length - 1" class="step-connector" aria-hidden="true"></span>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.section {
  padding: 96px 24px;
  scroll-margin-top: 60px;
  background: linear-gradient(180deg, transparent, rgba(0, 82, 217, 0.03) 30%, transparent);
}

.section-inner {
  max-width: 1200px;
  margin: 0 auto;
}

.section-head {
  text-align: center;
  max-width: 640px;
  margin: 0 auto 56px;
}

.section-title {
  font-size: 34px;
  font-weight: 800;
  letter-spacing: -0.02em;
  color: var(--land-text);
  margin-bottom: 14px;
}

.section-subtitle {
  font-size: 15px;
  line-height: 1.7;
  color: var(--land-text-2);
}

.steps-track {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  list-style: none;
}

.step {
  position: relative;
  background: var(--land-surface);
  border: 1px solid var(--land-border);
  border-radius: 16px;
  padding: 24px 22px;
  transition: transform var(--transition-slow), box-shadow var(--transition-slow),
    border-color var(--transition-slow);
}

.step:hover {
  transform: translateY(-4px);
  border-color: rgba(111, 91, 246, 0.35);
  box-shadow: 0 16px 40px -12px rgba(111, 91, 246, 0.16);
}

.step-number {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  background: linear-gradient(120deg, var(--land-brand), var(--land-brand-2));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.step-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--land-text);
  margin: 10px 0 8px;
}

.step-desc {
  font-size: 13px;
  line-height: 1.7;
  color: var(--land-text-2);
}

.step-connector {
  position: absolute;
  top: 50%;
  right: -20px;
  width: 20px;
  height: 1.5px;
  background: linear-gradient(90deg, var(--land-brand), var(--land-brand-2));
  opacity: 0.4;
}

@media (max-width: 960px) {
  .steps-track {
    grid-template-columns: repeat(2, 1fr);
  }

  .step-connector {
    display: none;
  }
}

@media (max-width: 620px) {
  .section {
    padding: 72px 20px;
  }

  .section-title {
    font-size: 26px;
  }

  .steps-track {
    grid-template-columns: 1fr;
  }
}
</style>
