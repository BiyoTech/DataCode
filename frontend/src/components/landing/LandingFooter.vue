<script setup lang="ts">
/**
 * LandingFooter — minimal footer with brand mark, tagline and copyright.
 */
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
</script>

<template>
  <footer class="landing-footer">
    <div class="footer-inner">
      <div class="footer-brand">
        <span class="brand-mark">&gt;_</span>
        <span class="brand-name">DataCode</span>
        <span class="footer-tagline">{{ t('landing.footer.tagline') }}</span>
      </div>
      <p class="footer-copy">{{ t('landing.footer.copyright') }}</p>
    </div>
  </footer>
</template>

<style scoped>
.landing-footer {
  border-top: 1px solid var(--land-border);
  background: color-mix(in srgb, var(--land-surface) 60%, transparent);
}

.footer-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 28px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.footer-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: linear-gradient(135deg, var(--land-brand), var(--land-brand-2));
  color: #fff;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
}

.brand-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--land-text);
}

.footer-tagline {
  font-size: 12px;
  color: var(--land-text-3);
  padding-left: 10px;
  border-left: 1px solid var(--land-border);
}

.footer-copy {
  font-size: 12px;
  color: var(--land-text-3);
}
</style>
