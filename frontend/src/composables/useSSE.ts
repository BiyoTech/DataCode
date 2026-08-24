/**
 * ACP event stream for an agent session.
 *
 * A thin layer over `useEventStream`: it fixes the URL to the session's event
 * endpoint and parses each payload as an `ACPEvent`. Everything about the
 * transport — bearer auth, 401 refresh, 403 termination, heartbeat, backoff —
 * lives in the generic composable and is shared with the workspace log tail.
 */
import type { ConnectionState, ACPEvent } from '@/types/agent'
import { useEventStream } from '@/composables/useEventStream'
import { notifyError } from '@/utils/notifyError'
import type { Ref } from 'vue'

export interface UseSSEOptions {
  /** Called on every parsed ACP event */
  onEvent: (event: ACPEvent) => void
  /** Called when connection state changes */
  onStateChange?: (state: ConnectionState) => void
  /** Max reconnect attempts (default: 5) */
  maxReconnectAttempts?: number
  /** Heartbeat timeout in ms — disconnect if no data received (default: 45000) */
  heartbeatTimeout?: number
}

export interface UseSSEReturn {
  connectionState: Ref<ConnectionState>
  connect: (sessionId: string) => void
  disconnect: () => void
}

export function useSSE(options: UseSSEOptions): UseSSEReturn {
  const { onEvent, onStateChange, maxReconnectAttempts, heartbeatTimeout } = options

  let currentSessionId = ''

  const stream = useEventStream({
    url: () => `/api/v1/sessions/${currentSessionId}/events`,
    onMessage: (eventType, rawData) => {
      try {
        const event = JSON.parse(rawData) as ACPEvent
        // eslint-disable-next-line no-console
        console.debug(
          `%c[SSE] ${event.type}`,
          'color:#0052d9;font-weight:bold',
          { event_id: event.event_id, sse_event: eventType || 'message', payload: event.payload },
        )
        onEvent(event)
      } catch (e) {
        notifyError(e, 'Failed to parse event stream')
        console.error('Failed to parse SSE event data:', e, rawData)
      }
    },
    onStateChange,
    maxReconnectAttempts,
    heartbeatTimeout,
    label: 'SSE',
  })

  return {
    connectionState: stream.connectionState,
    connect(sessionId: string) {
      currentSessionId = sessionId
      stream.connect()
    },
    disconnect: stream.disconnect,
  }
}
