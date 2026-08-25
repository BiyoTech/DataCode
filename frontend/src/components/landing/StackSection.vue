<script setup lang="ts">
/**
 * StackSection — the open-source foundation (SQLMesh / Dagster / Daft /
 * OpenAI Agents SDK) presented as mono-styled engine cards.
 */
import { useI18n } from 'vue-i18n'

import { vReveal } from '@/composables/useReveal'

const { t } = useI18n()

/** Ordered stack keys; each carries a two-letter monogram for its tile. */
const items = [
  { key: 'sqlmesh', mark: 'SM' },
  { key: 'dagster', mark: 'DG' },
  { key: 'daft', mark: 'DF' },
  { key: 'agents', mark: 'AI' },
] as const
</script>

<template>
  <section id="stack" class="section">
    <div class="section-inner">
      <div class="section-head" v-reveal>
        <h2 class="section-title">{{ t('landing.stack.title') }}</h2>
        <p class="section-subtitle">{{ t('landing.stack.subtitle') }}</p>
      </div>

      <div class="stack-grid">
        <article
          v-for="(item, index) in items"
          :key="item.key"
          class="stack-card"
          v-reveal="index * 70"
        >
          <span class="stack-mark">{{ item.mark }}</span>
          <div class="stack-text">
            <h3 class="stack-name">{{ t(`landing.stack.items.${item.key}.name`) }}</h3>
            <p class="stack-desc">{{ t(`landing.stack.items.${item.key}.desc`) }}</p>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>

<style scoped>
.section {
  padding: 96px 24px;
  scroll-margin-top: 60px;
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

.stack-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
}

.stack-card {
  display: flex;
  align-items: center;
  gap: 14px;
  background: var(--land-surface);
  border: 1px solid var(--land-border);
  border-radius: 14px;
  padding: 18px 20px;
  transition: transform var(--transition-slow), border-color var(--transition-slow),
    box-shadow var(--transition-slow);
}

.stack-card:hover {
  transform: translateY(-3px);
  border-color: rgba(0, 82, 217, 0.3);
  box-shadow: 0 12px 32px -10px rgba(16, 24, 40, 0.14);
}

.stack-mark {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, var(--land-brand), var(--land-brand-2));
}

.stack-name {
  font-family: var(--font-mono);
  font-size: 14px;
  font-weight: 700;
  color: var(--land-text);
}

.stack-desc {
  font-size: 12px;
  line-height: 1.55;
  color: var(--land-text-2);
  margin-top: 3px;
}

@media (max-width: 960px) {
  .stack-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 620px) {
  .section {
    padding: 72px 20px;
  }

  .section-title {
    font-size: 26px;
  }

  .stack-grid {
    grid-template-columns: 1fr;
  }
}
</style>
