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

/** Extra flag marking a request that has already been retried after a refresh. */
interface RetryableConfig extends InternalAxiosRequestConfig {
  _retriedAfterRefresh?: boolean
}

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

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
// replay the original request.
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetryableConfig | undefined

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
