<script setup lang="ts">
/**
 * LandingNav — fixed glassmorphism top bar for the landing page.
 * Shows anchor navigation, a locale toggle and a session-aware CTA
 * (Sign In for guests, Enter Workbench for authenticated users).
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { setLocale } from '@/i18n'
import { getSession } from '@/utils/authSession'

const { t, locale } = useI18n()
const router = useRouter()

const isAuthed = computed(() => getSession() !== null)

/** Label of the language the toggle will switch TO. */
const localeToggleLabel = computed(() => (locale.value === 'en' ? '中文' : 'EN'))

function toggleLocale() {
  setLocale(locale.value === 'en' ? 'zh-CN' : 'en')
}

/** Smooth-scroll to a landing section below the fixed nav. */
function scrollTo(sectionId: string) {
  document.getElementById(sectionId)?.scrollIntoView({ behavior: 'smooth' })
}

function handleCta() {
  router.push(isAuthed.value ? '/app' : '/login')
}
</script>

<template>
  <header class="landing-nav">
    <div class="nav-inner">
      <button class="brand" @click="scrollTo('hero')">
        <span class="brand-mark">&gt;_</span>
        <span class="brand-name">DataCode</span>
      </button>

      <nav class="nav-links">
        <button class="nav-link" @click="scrollTo('capabilities')">
          {{ t('landing.nav.capabilities') }}
        </button>
        <button class="nav-link" @click="scrollTo('workflow')">
          {{ t('landing.nav.workflow') }}
        </button>
        <button class="nav-link" @click="scrollTo('stack')">
          {{ t('landing.nav.stack') }}
        </button>
      </nav>

      <div class="nav-actions">
        <button class="locale-toggle" :title="localeToggleLabel" @click="toggleLocale">
          <t-icon name="translate" size="16px" />
          <span>{{ localeToggleLabel }}</span>
        </button>
        <button class="cta-btn" @click="handleCta">
          {{ isAuthed ? t('landing.nav.enterWorkbench') : t('landing.nav.signIn') }}
          <t-icon name="arrow-right" size="14px" />
        </button>
      </div>
    </div>
  </header>
</template>

<style scoped>
.landing-nav {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 50;
  background: color-mix(in srgb, var(--land-surface) 78%, transparent);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--land-border);
}

.nav-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  height: 60px;
  display: flex;
  align-items: center;
  gap: 32px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--land-brand), var(--land-brand-2));
  color: #fff;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
}

.brand-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--land-text);
  letter-spacing: -0.01em;
}

.nav-links {
  display: flex;
  gap: 4px;
  flex: 1;
}

.nav-link {
  background: none;
  border: none;
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--land-text-2);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast);
}

.nav-link:hover {
  color: var(--land-text);
  background: rgba(16, 24, 40, 0.05);
}

.nav-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.locale-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: none;
  border: 1px solid var(--land-border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--land-text-2);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    border-color var(--transition-fast);
}

.locale-toggle:hover {
  color: var(--land-brand);
  border-color: var(--land-brand);
}

.cta-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--land-brand);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background var(--transition-fast),
    transform var(--transition-fast),
    box-shadow var(--transition-fast);
}

.cta-btn:hover {
  background: var(--land-brand-hover);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(0, 82, 217, 0.25);
}

@media (max-width: 720px) {
  .nav-links {
    display: none;
  }
}
</style>
