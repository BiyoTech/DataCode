<script setup lang="ts">
/**
 * CapabilitiesSection — six core capability cards presented as a grid.
 * The two product pillars (data-as-code, AI-native) lead the order, followed
 * by the differentiating environment=branch model and trust/analysis cards.
 */
import { useI18n } from 'vue-i18n'

import { vReveal } from '@/composables/useReveal'

const { t } = useI18n()

/** Ordered capability keys paired with their TDesign icon names. */
const items = [
  { key: 'dataAsCode', icon: 'code' },
  { key: 'aiNative', icon: 'chat-bubble-1' },
  { key: 'envBranch', icon: 'git-branch' },
  { key: 'plan', icon: 'task-checked' },
  { key: 'analysis', icon: 'chart-line' },
  { key: 'governance', icon: 'shield' },
] as const
</script>

<template>
  <section id="capabilities" class="section">
    <div class="section-inner">
      <div class="section-head" v-reveal>
        <h2 class="section-title">{{ t('landing.capabilities.title') }}</h2>
        <p class="section-subtitle">{{ t('landing.capabilities.subtitle') }}</p>
      </div>

      <div class="cards-grid">
        <article
          v-for="(item, index) in items"
          :key="item.key"
          class="cap-card"
          v-reveal="index * 70"
        >
          <span class="cap-icon">
            <t-icon :name="item.icon" size="20px" />
          </span>
          <h3 class="cap-title">{{ t(`landing.capabilities.items.${item.key}.title`) }}</h3>
          <p class="cap-desc">{{ t(`landing.capabilities.items.${item.key}.desc`) }}</p>
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

.cards-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}

.cap-card {
  background: var(--land-surface);
  border: 1px solid var(--land-border);
  border-radius: 16px;
  padding: 26px 24px;
  transition: transform var(--transition-slow), border-color var(--transition-slow),
    box-shadow var(--transition-slow);
}

.cap-card:hover {
  transform: translateY(-4px);
  border-color: rgba(0, 82, 217, 0.35);
  box-shadow: 0 16px 40px -12px rgba(0, 82, 217, 0.16);
}

.cap-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  color: var(--land-brand);
  background: linear-gradient(135deg, rgba(0, 82, 217, 0.09), rgba(111, 91, 246, 0.09));
  border: 1px solid rgba(0, 82, 217, 0.14);
  margin-bottom: 18px;
}

.cap-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--land-text);
  margin-bottom: 8px;
}

.cap-desc {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--land-text-2);
}

@media (max-width: 960px) {
  .cards-grid {
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

  .cards-grid {
    grid-template-columns: 1fr;
  }
}
</style>
