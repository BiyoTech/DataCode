/**
 * Workspace relocation state (design 03 §4.3, contract 08).
 *
 * When a runtime replica drains, the working copies it holds move to another
 * replica. Requests routed to the draining holder answer 503
 * `WORKSPACE_RELOCATING` with a `Retry-After` header; the API client retries
 * them transparently, and THIS module is how the rest of the app learns that a
 * retry is in flight — so it can show one calm banner instead of every panel
 * independently failing and flashing its own error.
 *
 * Deliberately not a Pinia store: it is transport-layer state, not application
 * state. Pinia stores are reset per workspace switch; a relocation can span
 * one, and the axios interceptor (outside the Vue tree) is the writer.
 */
import { ref, readonly } from 'vue'

/** Number of requests currently waiting out a relocation. */
const pending = ref(0)

/** Bump when a request starts waiting; returns the matching decrement. */
export function relocationStarted(): () => void {
  pending.value += 1
  return () => {
    pending.value = Math.max(0, pending.value - 1)
  }
}

/** Whether any request is currently waiting for a working copy to move. */
export function isRelocating(): boolean {
  return pending.value > 0
}

/** Reactive view for components that need to render the state. */
export const relocationPending = readonly(pending)
