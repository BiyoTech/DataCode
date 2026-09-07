/**
 * Low-level Server-Sent Events client.
 *
 * Built on `fetch` + `ReadableStream` rather than `EventSource`, because the
 * platform authenticates with a bearer token and `EventSource` cannot carry
 * one. That choice brings the rest of the behaviour with it: a 401 triggers a
 * single session refresh, a 403 is terminal (retrying cannot grant a
 * permission), and a silent connection is treated as dead and reconnected with
 * exponential backoff.
 *
 * This composable knows nothing about payload shapes — it hands raw
 * `(eventType, data)` pairs to the caller. `useSSE` layers ACP event parsing
 * on top; the log panel parses its own lines.
 */
import { ref, onUnmounted, getCurrentInstance } from 'vue'
import type { Ref } from 'vue'
import type { ConnectionState } from '@/types/agent'
import { notifyError } from '@/utils/notifyError'
import { getAccessToken, refreshSession } from '@/utils/authSession'

export interface UseEventStreamOptions {
  /** Resolved lazily so a reconnect always uses the current parameters. */
  url: () => string
  /** Called for every complete SSE event with its raw `data` payload. */
  onMessage: (eventType: string, data: string) => void
  onStateChange?: (state: ConnectionState) => void
  /** Max reconnect attempts (default: 5, may be Infinity for endless retry) */
  maxReconnectAttempts?: number
  /** Disconnect and retry if no byte arrives within this window (default: 45000) */
  heartbeatTimeout?: number
  /** Short name used in console diagnostics. */
  label?: string
}

export interface UseEventStreamReturn {
  connectionState: Ref<ConnectionState>
  connect: () => void
  disconnect: () => void
}

export function useEventStream(options: UseEventStreamOptions): UseEventStreamReturn {
  const {
    url,
    onMessage,
    onStateChange,
    maxReconnectAttempts = 5,
    heartbeatTimeout = 45000,
    label = 'SSE',
  } = options

  const connectionState = ref<ConnectionState>('disconnected')
  let abortController: AbortController | null = null
  let reconnectAttempts = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  let intentionalClose = false

  function setConnectionState(state: ConnectionState) {
    connectionState.value = state
    onStateChange?.(state)
  }

  function _streamHeaders(): Record<string, string> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    const token = getAccessToken()
    if (token) {
      headers.Authorization = `Bearer ${token}`
    }
    return headers
  }

  function connect() {
    intentionalClose = false
    reconnectAttempts = 0
    _doConnect()
  }

  async function _doConnect() {
    if (abortController) {
      abortController.abort()
    }

    setConnectionState('connecting')
    abortController = new AbortController()

    const target = url()

    try {
      let response = await fetch(target, {
        method: 'GET',
        headers: _streamHeaders(),
        signal: abortController.signal,
      })

      if (response.status === 401) {
        const renewed = await refreshSession()
        if (renewed) {
          response = await fetch(target, {
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
        throw new Error(`${label} connect failed: ${response.status} ${response.statusText}`)
      }

      if (!response.body) {
        throw new Error(`${label} response body is null (ReadableStream not supported)`)
      }

      reconnectAttempts = 0
      setConnectionState('connected')
      _resetHeartbeatTimer()

      // eslint-disable-next-line no-console
      console.info(`%c[${label}] connected → ${target}`, 'color:#00a870;font-weight:bold')

      await _parseStream(response.body)
    } catch (err: any) {
      if (err.name === 'AbortError') {
        return
      }
      console.error(`${label} connection error:`, err)
      if (!intentionalClose) {
        _scheduleReconnect()
      } else {
        setConnectionState('disconnected')
      }
    }
  }

  /** Parse the SSE wire format: `event:`, `data:`, comments, blank delimiter. */
  async function _parseStream(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let currentEvent = ''
    let currentData = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        // Any byte proves the connection is alive.
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
            if (currentData) {
              onMessage(currentEvent, currentData)
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

    if (!intentionalClose) {
      _scheduleReconnect()
    } else {
      setConnectionState('disconnected')
    }
  }

  function _resetHeartbeatTimer() {
    if (heartbeatTimer) {
      clearTimeout(heartbeatTimer)
    }
    heartbeatTimer = setTimeout(() => {
      console.warn(`${label} heartbeat timeout, reconnecting...`)
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

    // Exponential backoff capped at 16s: 1s, 2s, 4s, 8s, 16s, 16s, ...
    // The cap is what makes an unlimited retry budget affordable — a dead
    // backend is polled once per 16s, not hammered.
    const delay = Math.min(1000 * Math.pow(2, Math.min(reconnectAttempts - 1, 4)), 16000)
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

  // Only components have an unmount hook; a store owns its own lifecycle.
  if (getCurrentInstance()) {
    onUnmounted(disconnect)
  }

  return {
    connectionState,
    connect,
    disconnect,
  }
}
