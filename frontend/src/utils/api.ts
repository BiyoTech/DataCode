/**
 * HTTP client (Axios) with default configuration.
 * All timestamps returned by the API are UTC; frontend converts for display.
 */
import axios, {
  AxiosError,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios'
import { extractApiMessage } from './notifyError'
import { clearSession, getAccessToken, refreshSession } from './authSession'
import { relocationStarted } from './relocating'

/** Extra flag marking a request that has already been retried after a refresh. */
interface RetryableConfig extends InternalAxiosRequestConfig {
  _retriedAfterRefresh?: boolean
}

/** Extra flag marking how many times a relocation was already waited out. */
interface RelocatingConfig extends RetryableConfig {
  _relocationRetries?: number
}

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Relocation retry policy (design 03 §4.3). The backend drains a runtime by
 * moving its working copies to another replica; requests that land on the
 * draining holder get 503 WORKSPACE_RELOCATING with a Retry-After hint. The
 * client waits and replays instead of surfacing an error, because from the
 * user's perspective nothing failed — the platform is just moving the file.
 *
 * Bounded so a permanently stuck drain surfaces as an error rather than an
 * endless spinner: the drain timeout on the server side is minutes, so a
 * hand-over that has not settled within this budget is an outage worth seeing.
 */
const RELOCATION_MAX_RETRIES = 4
const RELOCATION_RETRY_AFTER_CAP_MS = 10_000

function relocationWaitMs(error: AxiosError): number {
  const header = error.response?.headers?.['retry-after']
  const hinted = Number.parseInt(String(header ?? ''), 10)
  if (Number.isFinite(hinted) && hinted > 0) {
    return Math.min(hinted * 1000, RELOCATION_RETRY_AFTER_CAP_MS)
  }
  // No usable hint: 2s is short enough to feel instant on a fast hand-over and
  // gentle enough not to hammer the gateway when the drain is slow.
  return 2_000
}

function isRelocating(error: AxiosError): boolean {
  const body = error.response?.data as { error_code?: string } | undefined
  return (
    error.response?.status === 503 &&
    body?.error_code === 'WORKSPACE_RELOCATING'
  )
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Callback invoked when the session cannot be recovered, so the app can send
 * the user back to the login screen. Registered by the router to avoid a
 * circular import.
 */
let onUnauthenticated: (() => void) | null = null

export function setUnauthenticatedHandler(handler: () => void): void {
  onUnauthenticated = handler
}

// Request interceptor - attach trace ID and bearer credentials.
// The user identity itself is derived from the token by the gateway, which
// overwrites any client-supplied identity headers.
api.interceptors.request.use((config) => {
  config.headers['X-Trace-ID'] = crypto.randomUUID()
  const token = getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Response interceptor - silently refresh expired credentials once, then
// replay the original request. Relocations are waited out the same way: the
// platform is moving the working copy, so the right answer is patience.
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RelocatingConfig | undefined

    if (error.response?.status === 401 && config && !config._retriedAfterRefresh) {
      config._retriedAfterRefresh = true

      // Concurrent 401s share a single refresh round-trip.
      const renewed = await refreshSession()
      if (renewed) {
        return api(withFreshToken(config, renewed.tokens.accessToken))
      }

      clearSession()
      onUnauthenticated?.()
      return Promise.reject(error)
    }

    // A draining runtime is moving this working copy to another replica. Wait
    // out the hand-over and replay — the user should see a relocation banner,
    // not an error toast. The pending counter drives that banner.
    if (config && isRelocating(error)) {
      const retries = config._relocationRetries ?? 0
      if (retries < RELOCATION_MAX_RETRIES) {
        config._relocationRetries = retries + 1
        const done = relocationStarted()
        try {
          await delay(relocationWaitMs(error))
        } finally {
          done()
        }
        return api(config)
      }
    }

    const message = extractApiMessage(error)
    console.error('[API Error]', message)
    return Promise.reject(error)
  }
)

function withFreshToken(config: RetryableConfig, accessToken: string): AxiosRequestConfig {
  return {
    ...config,
    headers: { ...config.headers, Authorization: `Bearer ${accessToken}` },
  }
}

export default api
