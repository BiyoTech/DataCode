<script setup lang="ts">
/**
 * HeroSection — landing hero: positioning headline, session-aware CTAs and a
 * CSS-built product preview (chat → generated code → commit → plan) that
 * communicates "data as code, written by AI" at a glance.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { vReveal } from '@/composables/useReveal'
import { getSession } from '@/utils/authSession'

const { t } = useI18n()
const router = useRouter()

const isAuthed = computed(() => getSession() !== null)

function handlePrimary() {
  router.push(isAuthed.value ? '/app' : '/login')
}

function scrollToCapabilities() {
  document.getElementById('capabilities')?.scrollIntoView({ behavior: 'smooth' })
}
</script>

<template>
  <section id="hero" class="hero-section">
    <div class="hero-inner">
      <div class="hero-copy">
        <span class="hero-badge" v-reveal>
          <span class="badge-dot"></span>
          {{ t('landing.hero.badge') }}
        </span>

        <h1 class="hero-title" v-reveal="80">
          {{ t('landing.hero.titleA') }}
          <br />
          <span class="hero-title-gradient">{{ t('landing.hero.titleB') }}</span>
        </h1>

        <p class="hero-subtitle" v-reveal="160">{{ t('landing.hero.subtitle') }}</p>

        <div class="hero-ctas" v-reveal="240">
          <button class="cta-primary" @click="handlePrimary">
            {{ isAuthed ? t('landing.hero.primaryCtaAuthed') : t('landing.hero.primaryCta') }}
            <t-icon name="arrow-right" size="15px" />
          </button>
          <button class="cta-secondary" @click="scrollToCapabilities">
            {{ t('landing.hero.secondaryCta') }}
          </button>
        </div>
      </div>

      <!-- Product preview: prompt in, reviewed plan out. -->
      <div class="hero-visual" v-reveal="200">
        <div class="preview-card">
          <div class="preview-chrome">
            <span class="chrome-dot"></span>
            <span class="chrome-dot"></span>
            <span class="chrome-dot"></span>
            <span class="preview-file">models/marts/daily_revenue.sql</span>
          </div>

          <div class="preview-body">
            <div class="chat-line">
              <span class="chat-avatar">U</span>
              <p class="chat-text">{{ t('landing.hero.previewUser') }}</p>
            </div>

            <div class="chat-line">
              <span class="chat-avatar ai">AI</span>
              <div class="chat-main">
                <pre class="code-block"><code><span class="tk-k">MODEL</span> (
  <span class="tk-p">name</span> marts.daily_revenue,
  <span class="tk-p">kind</span> <span class="tk-k">INCREMENTAL_BY_TIME_RANGE</span> (time_column ds)
);

<span class="tk-k">SELECT</span> ds, region, <span class="tk-f">SUM</span>(amount) <span class="tk-k">AS</span> revenue
<span class="tk-k">FROM</span> raw.orders
<span class="tk-k">WHERE</span> ds <span class="tk-k">BETWEEN</span> <span class="tk-v">@start_ds</span> <span class="tk-k">AND</span> <span class="tk-v">@end_ds</span>
<span class="tk-k">GROUP BY</span> <span class="tk-n">1</span>, <span class="tk-n">2</span></code></pre>
                <p class="chat-note">{{ t('landing.hero.previewAssistant') }}</p>
              </div>
            </div>
          </div>

          <div class="preview-footer">
            <span class="plan-badge">
              <span class="plan-dot"></span>
              {{ t('landing.hero.previewPlanReady') }}
            </span>
            <span class="preview-cmd">$ git commit -m "feat: add daily_revenue"</span>
          </div>
        </div>

        <!-- Floating accent chips for spatial depth. -->
        <div class="float-chip chip-lineage" aria-hidden="true">
          <span class="chip-node"></span>
          <span class="chip-edge"></span>
          <span class="chip-node"></span>
          <span class="chip-edge"></span>
          <span class="chip-node accent"></span>
        </div>
        <div class="float-chip chip-audit" aria-hidden="true">
          <t-icon name="task-checked" size="13px" />
          <span>audits · 3/3</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hero-section {
  padding: 148px 24px 96px;
  scroll-margin-top: 60px;
}

.hero-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: 64px;
  align-items: center;
}

.hero-copy {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 24px;
}

.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--land-border);
  background: var(--land-surface);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--land-brand);
  box-shadow: 0 1px 4px rgba(16, 24, 40, 0.05);
}

.badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--land-brand);
  animation: pulse-dot 2.4s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0, 82, 217, 0.35); }
  50% { box-shadow: 0 0 0 5px rgba(0, 82, 217, 0); }
}

.hero-title {
  font-size: 52px;
  line-height: 1.12;
  font-weight: 800;
  letter-spacing: -0.03em;
  color: var(--land-text);
}

.hero-title-gradient {
  background: linear-gradient(120deg, var(--land-brand) 10%, var(--land-brand-2) 90%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-subtitle {
  font-size: 16px;
  line-height: 1.75;
  color: var(--land-text-2);
  max-width: 540px;
}

.hero-ctas {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}

.cta-primary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: var(--land-brand);
  color: #fff;
  border: none;
  border-radius: 10px;
  padding: 13px 26px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: background var(--transition-fast), transform var(--transition-fast),
    box-shadow var(--transition-fast);
}

.cta-primary:hover {
  background: var(--land-brand-hover);
  transform: translateY(-2px);
  box-shadow: 0 10px 24px rgba(0, 82, 217, 0.28);
}

.cta-secondary {
  background: var(--land-surface);
  color: var(--land-text);
  border: 1px solid var(--land-border);
  border-radius: 10px;
  padding: 13px 26px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast),
    transform var(--transition-fast);
}

.cta-secondary:hover {
  border-color: var(--land-brand);
  color: var(--land-brand);
  transform: translateY(-2px);
}

/* === Product preview mock === */
.hero-visual {
  position: relative;
}

.preview-card {
  position: relative;
  background: var(--land-surface);
  border: 1px solid var(--land-border);
  border-radius: 16px;
  box-shadow: 0 24px 64px -16px rgba(16, 24, 40, 0.14), 0 4px 16px rgba(16, 24, 40, 0.06);
  overflow: hidden;
  animation: hero-float 7s ease-in-out infinite;
}

@keyframes hero-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-8px); }
}

.preview-chrome {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--land-border);
  background: rgba(16, 24, 40, 0.02);
}

.chrome-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--land-border);
}

.preview-file {
  margin-left: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--land-text-3);
}

.preview-body {
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.chat-line {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.chat-avatar {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  background: rgba(16, 24, 40, 0.06);
  color: var(--land-text-2);
}

.chat-avatar.ai {
  background: linear-gradient(135deg, var(--land-brand), var(--land-brand-2));
  color: #fff;
}

.chat-text {
  font-size: 13px;
  line-height: 1.6;
  color: var(--land-text);
  background: rgba(16, 24, 40, 0.04);
  border-radius: 4px 12px 12px 12px;
  padding: 8px 12px;
}

.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.code-block {
  background: #f8fafd;
  border: 1px solid var(--land-border);
  border-radius: 10px;
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--land-text-2);
  overflow-x: auto;
  white-space: pre;
}

.tk-k { color: var(--land-brand); font-weight: 600; }
.tk-p { color: var(--land-cyan); }
.tk-f { color: var(--land-brand-2); }
.tk-v { color: #b54708; }
.tk-n { color: #027a48; }

.chat-note {
  font-size: 12px;
  line-height: 1.6;
  color: var(--land-text-2);
}

.preview-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--land-border);
  background: rgba(16, 24, 40, 0.02);
}

.plan-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: #027a48;
}

.plan-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #12b76a;
}

.preview-cmd {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--land-text-3);
}

/* === Floating accent chips === */
.float-chip {
  position: absolute;
  display: inline-flex;
  align-items: center;
  background: var(--land-surface);
  border: 1px solid var(--land-border);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(16, 24, 40, 0.1);
}

.chip-lineage {
  top: -18px;
  right: -12px;
  padding: 10px 14px;
  gap: 0;
  animation: chip-float 5s ease-in-out infinite;
}

.chip-node {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 2px solid var(--land-brand);
  background: #fff;
}

.chip-node.accent {
  border-color: var(--land-brand-2);
  background: var(--land-brand-2);
}

.chip-edge {
  width: 14px;
  height: 1.5px;
  background: linear-gradient(90deg, var(--land-brand), var(--land-brand-2));
}

.chip-audit {
  bottom: -14px;
  left: -16px;
  padding: 8px 12px;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  color: #027a48;
  animation: chip-float 5s ease-in-out 0.8s infinite;
}

.chip-audit .t-icon {
  color: #12b76a;
}

@keyframes chip-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}

@media (max-width: 960px) {
  .hero-section {
    padding: 120px 24px 72px;
  }

  .hero-inner {
    grid-template-columns: 1fr;
    gap: 48px;
  }

  .hero-title {
    font-size: 38px;
  }

  .hero-visual {
    max-width: 560px;
  }

  .float-chip {
    display: none;
  }
}

@media (max-width: 480px) {
  .hero-title {
    font-size: 30px;
  }

  .hero-ctas {
    flex-wrap: wrap;
  }
}
</style>
