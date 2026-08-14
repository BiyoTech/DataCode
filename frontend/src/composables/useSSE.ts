/**
 * SSE connection composable — fetch + ReadableStream based SSE parsing.
 * Implements auto-reconnect with exponential backoff, heartbeat timeout detection,
 * and connection state management. Replaces useWebSocket for ACP v2 event streaming.
 */
import { ref, onUnmounted } from 'vue'
import type { ConnectionState, ACPEvent } from '@/types/agent'
import { notifyError } from '@/utils/notifyError'
import { getAccessToken, refreshSession } from '@/utils/authSession'

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
  connectionState: ReturnType<typeof ref<ConnectionState>>
  connect: (sessionId: string) => void
  disconnect: () => void
}

export function useSSE(options: UseSSEOptions): UseSSEReturn {
  const {
    onEvent,
    onStateChange,
    maxReconnectAttempts = 5,
    heartbeatTimeout = 45000,
  } = options

  const connectionState = ref<ConnectionState>('disconnected')
  let abortController: AbortController | null = null
  let reconnectAttempts = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  let intentionalClose = false
  let currentSessionId = ''

  function setConnectionState(state: ConnectionState) {
    connectionState.value = state
    onStateChange?.(state)
  }

  /** Headers for the event stream request, including bearer credentials. */
  function _streamHeaders(): Record<string, string> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    const token = getAccessToken()
    if (token) {
      headers.Authorization = `Bearer ${token}`
    }
    return headers
  }

  function connect(sessionId: string) {
    intentionalClose = false
    reconnectAttempts = 0
    currentSessionId = sessionId
    _doConnect()
  }

  async function _doConnect() {
    // Abort previous connection if any
    if (abortController) {
      abortController.abort()
    }

    setConnectionState('connecting')
    abortController = new AbortController()

    const url = `/api/v1/sessions/${currentSessionId}/events`

    try {
      // The stream is opened with fetch (not EventSource) so the bearer token
      // can be attached; an expired token is refreshed once before retrying.
      let response = await fetch(url, {
        method: 'GET',
        headers: _streamHeaders(),
        signal: abortController.signal,
      })

      if (response.status === 401) {
        const renewed = await refreshSession()
        if (renewed) {
          response = await fetch(url, {
            method: 'GET',
            headers: _streamHeaders(),
            signal: abortController.signal,
          })
        }
      }

      // Authorization failures are terminal: retrying cannot grant a
      // permission, so stop and state exactly what is missing instead of
      // looping silently in the background.
      if (response.status === 403) {
        const body = await response.json().catch(() => null)
        notifyError({ response: { status: 403, data: body } })
        setConnectionState('disconnected')
        return
      }

      if (!response.ok) {
        throw new Error(`SSE connect failed: ${response.status} ${response.statusText}`)
      }

      if (!response.body) {
        throw new Error('SSE response body is null (ReadableStream not supported)')
      }

      reconnectAttempts = 0
      setConnectionState('connected')
      _resetHeartbeatTimer()

      // eslint-disable-next-line no-console
      console.info(`%c[SSE] connected → ${url}`, 'color:#00a870;font-weight:bold')

      // Parse SSE stream using ReadableStream
      await _parseSSEStream(response.body)
    } catch (err: any) {
      if (err.name === 'AbortError') {
        // Intentional abort — don't reconnect
        return
      }
      console.error('SSE connection error:', err)
      if (!intentionalClose) {
        _scheduleReconnect()
      } else {
        setConnectionState('disconnected')
      }
    }
  }

  /**
   * Parse SSE stream from ReadableStream using TextDecoder.
   * Handles standard SSE format: event, data, comments (heartbeat).
   */
  async function _parseSSEStream(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let currentEvent = ''
    let currentData = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        // Reset heartbeat timer on any data received
        _resetHeartbeatTimer()

        buffer += decoder.decode(value, { stream: true })

        // Split on \n; the trailing \r (CRLF streams) is stripped per-line below.
        const lines = buffer.split('\n')
        // Keep the incomplete last line in the buffer for the next chunk.
        buffer = lines.pop() || ''

        for (const rawLine of lines) {
          // Normalise CRLF: drop a trailing \r so the empty-line delimiter and
          // the JSON payload are not corrupted by stray carriage returns.
          const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine

          if (line.startsWith(':')) {
            // SSE comment (heartbeat) — ignore content, timer already reset.
            continue
          }

          if (line.startsWith('event:')) {
            // The single space after the colon is optional per the SSE spec.
            currentEvent = line.slice(6).replace(/^ /, '')
          } else if (line.startsWith('data:')) {
            // Optional leading space; multiple data lines join with a newline.
            const chunk = line.slice(5).replace(/^ /, '')
            currentData = currentData === '' ? chunk : currentData + '\n' + chunk
          } else if (line === '') {
            // Empty line = end of event.
            if (currentData) {
              _handleEventData(currentEvent, currentData)
            }
            currentEvent = ''
            currentData = ''
          }
        }
      }
    } catch (err: any) {
      if (err.name === 'AbortError') return
      throw err
    } finally {
      reader.releaseLock()
    }

    // Stream ended — schedule reconnect if not intentional
    if (!intentionalClose) {
      _scheduleReconnect()
    } else {
      setConnectionState('disconnected')
    }
  }

  function _handleEventData(eventType: string, rawData: string) {
    try {
      const event = JSON.parse(rawData) as ACPEvent
      // Debug log: surface every received SSE event in the console.
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
  }

  function _resetHeartbeatTimer() {
    if (heartbeatTimer) {
      clearTimeout(heartbeatTimer)
    }
    heartbeatTimer = setTimeout(() => {
      // No data received within timeout — connection is stale
      console.warn('SSE heartbeat timeout, reconnecting...')
      if (!intentionalClose) {
        _reconnect()
      }
    }, heartbeatTimeout)
  }

  function _stopHeartbeatTimer() {
    if (heartbeatTimer) {
      clearTimeout(heartbeatTimer)
      heartbeatTimer = null
    }
  }

  function _reconnect() {
    // Abort current connection and try again
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    _scheduleReconnect()
  }

  function _scheduleReconnect() {
    _stopHeartbeatTimer()

    if (reconnectAttempts >= maxReconnectAttempts) {
      setConnectionState('disconnected')
      return
    }

    setConnectionState('reconnecting')
    reconnectAttempts++

    // Exponential backoff: 1s, 2s, 4s, 8s, 16s
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 16000)
    reconnectTimer = setTimeout(() => {
      _doConnect()
    }, delay)
  }

  function disconnect() {
    intentionalClose = true
    _stopHeartbeatTimer()

    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }

    if (abortController) {
      abortController.abort()
      abortController = null
    }

    setConnectionState('disconnected')
  }

  // Auto-cleanup on component unmount
  onUnmounted(() => {
    disconnect()
  })

  return {
    connectionState,
    connect,
    disconnect,
  }
}
