/**
 * v-reveal directive — lightweight fade-up entrance animation triggered
 * when an element scrolls into the viewport.
 *
 * Usage:
 *   <div v-reveal>          — reveal with no delay
 *   <div v-reveal="120">    — reveal with a 120ms stagger delay
 *
 * The companion `.reveal` / `.is-revealed` CSS lives in LandingView.vue
 * (non-scoped, shared by every landing section).
 */
import type { Directive } from 'vue'

// One shared observer for all revealed elements on the page.
const revealObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-revealed')
        revealObserver.unobserve(entry.target)
      }
    }
  },
  { threshold: 0.15, rootMargin: '0px 0px -40px 0px' },
)

export const vReveal: Directive<HTMLElement, number | undefined> = {
  mounted(el, binding) {
    el.classList.add('reveal')
    if (typeof binding.value === 'number') {
      el.style.setProperty('--reveal-delay', `${binding.value}ms`)
    }
    revealObserver.observe(el)
  },
  unmounted(el) {
    revealObserver.unobserve(el)
  },
}
