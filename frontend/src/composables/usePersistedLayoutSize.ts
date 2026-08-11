/**
 * Persist a numeric layout dimension in localStorage with clamping.
 */
import { ref } from 'vue'

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

export function loadStoredNumber(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    const n = Number(raw)
    return Number.isFinite(n) ? n : fallback
  } catch {
    return fallback
  }
}

export function storeNumber(key: string, value: number) {
  try {
    localStorage.setItem(key, String(value))
  } catch {
    // localStorage may be disabled — ignore.
  }
}

export function usePersistedLayoutSize(
  key: string,
  fallback: number,
  min: number,
  max: number,
) {
  const size = ref(clamp(loadStoredNumber(key, fallback), min, max))

  function setSize(value: number) {
    const next = clamp(value, min, max)
    size.value = next
    storeNumber(key, next)
  }

  function applyDelta(delta: number) {
    setSize(size.value + delta)
  }

  return { size, setSize, applyDelta }
}
