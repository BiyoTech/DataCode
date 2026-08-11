/**
 * Internationalization (i18n) configuration.
 * Supports: English (en), Chinese (zh-CN).
 * Language detection: browser preference → localStorage → fallback to 'en'.
 */
import { createI18n } from 'vue-i18n'
import en from './locales/en'
import zhCN from './locales/zh-CN'

// Detect user's preferred language
function detectLanguage(): string {
  // 1. Check localStorage preference
  const stored = localStorage.getItem('locale')
  if (stored && ['en', 'zh-CN'].includes(stored)) {
    return stored
  }

  // 2. Check browser language
  const browserLang = navigator.language
  if (browserLang.startsWith('zh')) {
    return 'zh-CN'
  }

  // 3. Fallback to English
  return 'en'
}

export const i18n = createI18n({
  legacy: false, // Use Composition API mode
  locale: detectLanguage(),
  fallbackLocale: 'en',
  messages: {
    en,
    'zh-CN': zhCN,
  },
})

/**
 * Set locale and persist to localStorage.
 */
export function setLocale(locale: string) {
  ;(i18n.global.locale as any).value = locale
  localStorage.setItem('locale', locale)
  document.querySelector('html')?.setAttribute('lang', locale)
}
