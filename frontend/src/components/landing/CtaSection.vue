<script setup lang="ts">
/**
 * CtaSection — closing call-to-action band with a gradient frame.
 * Button is session-aware: guests go to sign-up, users to the workbench.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { vReveal } from '@/composables/useReveal'
import { getSession } from '@/utils/authSession'

const { t } = useI18n()
const router = useRouter()

const isAuthed = computed(() => getSession() !== null)

function handleClick() {
  router.push(isAuthed.value ? '/app' : '/login')
}
</script>

<template>
  <section class="cta-section">
    <div class="cta-band" v-reveal>
      <div class="cta-glow" aria-hidden="true"></div>
      <h2 class="cta-title">{{ t('landing.cta.title') }}</h2>
      <p class="cta-subtitle">{{ t('landing.cta.subtitle') }}</p>
      <button class="cta-button" @click="handleClick">
        {{ isAuthed ? t('landing.nav.enterWorkbench') : t('landing.cta.button') }}
        <t-icon name="arrow-right" size="15px" />
      </button>
    </div>
  </section>
</template>

<style scoped>
.cta-section {
  padding: 48px 24px 96px;
}

.cta-band {
  position: relative;
  max-width: 960px;
  margin: 0 auto;
  text-align: center;
  background: var(--land-surface);
  border: 1px solid rgba(0, 82, 217, 0.2);
  border-radius: 24px;
  padding: 64px 40px;
  overflow: hidden;
  box-shadow: 0 24px 64px -24px rgba(0, 82, 217, 0.25);
}

.cta-glow {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(50% 60% at 50% 0%, rgba(0, 82, 217, 0.1), transparent 70%),
    radial-gradient(40% 50% at 80% 100%, rgba(111, 91, 246, 0.1), transparent 70%);
  pointer-events: none;
}

.cta-title {
  position: relative;
  font-size: 30px;
  font-weight: 800;
  letter-spacing: -0.02em;
  color: var(--land-text);
  margin-bottom: 12px;
}

.cta-subtitle {
  position: relative;
  font-size: 15px;
  color: var(--land-text-2);
  margin-bottom: 28px;
}

.cta-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: var(--land-brand);
  color: #fff;
  border: none;
  border-radius: 10px;
  padding: 13px 30px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: background var(--transition-fast), transform var(--transition-fast),
    box-shadow var(--transition-fast);
}

.cta-button:hover {
  background: var(--land-brand-hover);
  transform: translateY(-2px);
  box-shadow: 0 10px 24px rgba(0, 82, 217, 0.28);
}

@media (max-width: 620px) {
  .cta-band {
    padding: 48px 24px;
  }

  .cta-title {
    font-size: 24px;
  }
}
</style>
