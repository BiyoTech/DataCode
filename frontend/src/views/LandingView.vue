<script setup lang="ts">
/**
 * LandingView — public product homepage at "/".
 *
 * Visual style is intentionally FIXED to a light, clean tech aesthetic and
 * does NOT follow the user's workbench theme (per product decision), so all
 * colors come from the `--land-*` tokens defined below instead of --td-*.
 *
 * The page owns its own scroll context because the global body is
 * overflow:hidden (the workbench is a fixed-viewport IDE).
 */
import LandingNav from '@/components/landing/LandingNav.vue'
import HeroSection from '@/components/landing/HeroSection.vue'
import CapabilitiesSection from '@/components/landing/CapabilitiesSection.vue'
import WorkflowSection from '@/components/landing/WorkflowSection.vue'
import StackSection from '@/components/landing/StackSection.vue'
import CtaSection from '@/components/landing/CtaSection.vue'
import LandingFooter from '@/components/landing/LandingFooter.vue'
</script>

<template>
  <div class="landing-page">
    <!-- Fixed decorative backdrop: fine grid + soft brand glows. -->
    <div class="landing-bg" aria-hidden="true"></div>

    <LandingNav />

    <main class="landing-main">
      <HeroSection />
      <CapabilitiesSection />
      <WorkflowSection />
      <StackSection />
      <CtaSection />
    </main>

    <LandingFooter />
  </div>
</template>

<!-- Landing theme tokens + shared reveal animation (used by child sections). -->
<style>
.landing-page {
  /* Fixed light-tech palette — independent of the app theme-mode. */
  --land-bg: #f5f7fb;
  --land-surface: #ffffff;
  --land-border: #e3e8f2;
  --land-text: #101828;
  --land-text-2: #475467;
  --land-text-3: #98a2b3;
  --land-brand: #0052d9;
  --land-brand-hover: #0065ff;
  --land-brand-2: #6f5bf6;
  --land-cyan: #0096bd;

  position: relative;
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--land-bg);
  color: var(--land-text);
  scroll-behavior: smooth;
}

.landing-bg {
  position: fixed;
  inset: 0;
  pointer-events: none;
  background-image:
    /* Fine engineering grid, faded towards the bottom. */
    linear-gradient(to right, rgba(16, 24, 40, 0.04) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(16, 24, 40, 0.04) 1px, transparent 1px),
    /* Soft brand glows. */
    radial-gradient(55% 40% at 50% -5%, rgba(0, 82, 217, 0.1), transparent 70%),
    radial-gradient(35% 30% at 88% 32%, rgba(111, 91, 246, 0.08), transparent 70%),
    radial-gradient(30% 26% at 8% 58%, rgba(0, 150, 189, 0.06), transparent 70%);
  background-size:
    56px 56px,
    56px 56px,
    100% 100%,
    100% 100%,
    100% 100%;
  mask-image: linear-gradient(to bottom, black 0%, black 55%, rgba(0, 0, 0, 0.35) 100%);
  -webkit-mask-image: linear-gradient(to bottom, black 0%, black 55%, rgba(0, 0, 0, 0.35) 100%);
}

.landing-main {
  position: relative;
}

/* === Shared scroll-reveal animation (driven by the v-reveal directive) === */
.reveal {
  opacity: 0;
  transform: translateY(22px);
  transition: opacity 0.6s ease, transform 0.6s cubic-bezier(0.22, 0.61, 0.36, 1);
  transition-delay: var(--reveal-delay, 0ms);
  will-change: opacity, transform;
}

.reveal.is-revealed {
  opacity: 1;
  transform: translateY(0);
}

@media (prefers-reduced-motion: reduce) {
  .landing-page {
    scroll-behavior: auto;
  }

  .reveal {
    opacity: 1;
    transform: none;
    transition: none;
  }
}
</style>
