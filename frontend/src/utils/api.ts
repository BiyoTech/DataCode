/**
 * HTTP client (Axios) with default configuration.
 * All timestamps returned by the API are UTC; frontend converts for display.
 */
import axios from 'axios'
import { extractApiMessage } from './notifyError'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor - add trace ID and user context
api.interceptors.request.use((config) => {
  config.headers['X-Trace-ID'] = crypto.randomUUID()
  let userId = 'user-admin-001'
  try {
    const stored = localStorage.getItem('auth_user')
    if (stored) {
      const parsed = JSON.parse(stored) as { userId?: string }
      if (parsed.userId) userId = parsed.userId
    }
  } catch {
    // Ignore malformed auth cache.
  }
  config.headers['X-User-ID'] = userId
  return config
})

// Response interceptor - handle errors uniformly
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const message = extractApiMessage(error)
    console.error('[API Error]', message)
    return Promise.reject(error)
  }
)

export default api
