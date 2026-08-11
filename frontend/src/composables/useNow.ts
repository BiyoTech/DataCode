/**
 * Global "now" ticker.
 *
 * A single interval (30s) drives a shared reactive ref so any component that
 * needs to recompute a relative time string ("3 minutes ago") can depend on
 * `now` and re-render as time passes — without each component spinning up its
 * own timer. The interval is lazily started on first subscription and cleared
 * when the last consumer unmounts.
 */
import { ref, onScopeDispose } from 'vue'

const TICK_INTERVAL_MS = 30_000

const now = ref(Date.now())
let refCount = 0
let timer: ReturnType<typeof setInterval> | null = null

function start() {
  if (timer !== null) return
  timer = setInterval(() => {
    now.value = Date.now()
  }, TICK_INTERVAL_MS)
}

function stop() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

/**
 * Subscribe to the global now tick. Returns a reactive ms timestamp that
 * updates every 30 seconds. Auto-unsubscribes when the calling scope is
 * disposed.
 */
export function useNow() {
  refCount++
  start()
  onScopeDispose(() => {
    refCount = Math.max(0, refCount - 1)
    if (refCount === 0) {
      stop()
    }
  })
  return now
}
