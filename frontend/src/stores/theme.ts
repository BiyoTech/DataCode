/**
 * Theme store - manages dark/light theme mode.
 * Persists preference to localStorage and syncs <html theme-mode> attribute
 * (TDesign's native theme switching mechanism).
 * Supports three modes: 'system' (follows OS), 'dark', 'light'.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export type ThemeMode = 'system' | 'dark' | 'light'
export type ResolvedTheme = 'dark' | 'light'

const STORAGE_KEY = 'theme_mode'

function getSystemPreference(): ResolvedTheme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/**
 * Apply theme to document element using TDesign's native theme-mode attribute.
 * TDesign reads theme-mode from <html> to switch between light/dark token sets.
 */
function applyHtmlTheme(resolved: ResolvedTheme): void {
  document.documentElement.setAttribute('theme-mode', resolved)
}

export const useThemeStore = defineStore('theme', () => {
  // Load persisted preference
  const stored = localStorage.getItem(STORAGE_KEY)
  const initial: ThemeMode = (stored === 'system' || stored === 'dark' || stored === 'light')
    ? stored
    : 'system'

  const mode = ref<ThemeMode>(initial)

  // Resolved effective theme
  const resolved = computed<ResolvedTheme>(() => {
    if (mode.value === 'system') {
      return getSystemPreference()
    }
    return mode.value
  })

  // Whether current effective theme is dark
  const isDark = computed(() => resolved.value === 'dark')

  /**
   * Set theme mode and persist.
   */
  function setMode(newMode: ThemeMode): void {
    mode.value = newMode
    const effective = newMode === 'system' ? getSystemPreference() : newMode
    applyHtmlTheme(effective)
    localStorage.setItem(STORAGE_KEY, newMode)
  }

  /**
   * Toggle between dark and light (keeps mode pinned, not system).
   */
  function toggle(): void {
    setMode(mode.value === 'dark' ? 'light' : 'dark')
  }

  // System preference change listener (only relevant when mode === 'system')
  function onSystemChange(e: MediaQueryListEvent): void {
    if (mode.value === 'system') {
      applyHtmlTheme(e.matches ? 'dark' : 'light')
    }
  }

  // Initialize: apply theme on store creation
  const effective = initial === 'system' ? getSystemPreference() : initial
  applyHtmlTheme(effective)

  // Listen for system preference changes
  const mql = window.matchMedia('(prefers-color-scheme: dark)')
  mql.addEventListener('change', onSystemChange)

  return {
    mode,
    resolved,
    isDark,
    setMode,
    toggle,
  }
})
