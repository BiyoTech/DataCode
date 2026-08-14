/**
 * Authentication session module.
 *
 * Owns the persisted credential pair and the single-flight refresh logic.
 * It lives outside the Pinia store on purpose: the axios interceptor in
 * `api.ts` must be able to refresh tokens without importing the store
 * (which would create a circular dependency).
 *
 * All timestamps exchanged with the backend are UTC; conversion for display
 * happens in the presentation layer.
 */
import axios from 'axios'

/**
 * Role code of a user inside one tenant.
 *
 * Roles are database-defined and tenant-editable (custom roles included), so the
 * frontend never enumerates them: it displays what the backend returns and gates
 * the UI on permission codes instead.
 */
export type TenantRole = string

export interface AuthTokens {
  accessToken: string
  refreshToken: string
  /** Access token expiry, UTC ISO 8601. */
  expiresAt: string
}

/** The active identity: one user in the context of one tenant. */
export interface UserInfo {
  userId: string
  username: string
  tenantId: string
  tenantName: string
  role: TenantRole
}

export interface AuthResult {
  user: UserInfo
  tokens: AuthTokens
}

/** One selectable identity in the login picker. */
export interface MembershipInfo {
  tenantId: string
  tenantName: string
  role: TenantRole
  /** True when the user created this tenant (primary account). */
  isOwner: boolean
  /** True for the identity used during the previous login. */
  isLastUsed: boolean
  /** Owner username, displayed on sub-account cards. */
  ownerUsername: string
}

/** Response of phase one of the login flow. */
export interface LoginResult {
  requiresTenantSelection: boolean
  preAuthToken?: string
  memberships?: MembershipInfo[]
  auth?: AuthResult
}

export interface AuthSession {
  user: UserInfo
  tokens: AuthTokens
}

const SESSION_STORAGE_KEY = 'auth_session'

/**
 * Bare axios client used for authentication calls. It deliberately has no
 * interceptors so refresh attempts can never recurse into themselves.
 */
const authClient = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

let session: AuthSession | null = loadSession()
/** Shared promise so concurrent 401s trigger only one refresh round-trip. */
let refreshPromise: Promise<AuthSession | null> | null = null
/** Listeners notified whenever the session is replaced or cleared. */
const sessionListeners = new Set<(session: AuthSession | null) => void>()

function loadSession(): AuthSession | null {
  const raw = localStorage.getItem(SESSION_STORAGE_KEY)
  if (!raw) return null

  const parsed = safeParse(raw)
  if (!parsed?.tokens?.accessToken || !parsed?.tokens?.refreshToken || !parsed?.user?.userId) {
    localStorage.removeItem(SESSION_STORAGE_KEY)
    return null
  }
  return parsed
}

function safeParse(raw: string): AuthSession | null {
  try {
    return JSON.parse(raw) as AuthSession
  } catch (error) {
    console.error('[auth] Failed to parse the stored session', error)
    return null
  }
}

function notifySessionChange() {
  sessionListeners.forEach((listener) => listener(session))
}

/** Subscribe to session changes; returns an unsubscribe function. */
export function onSessionChange(listener: (session: AuthSession | null) => void): () => void {
  sessionListeners.add(listener)
  return () => sessionListeners.delete(listener)
}

/** Current session, or null when unauthenticated. */
export function getSession(): AuthSession | null {
  return session
}

/** Access token for the Authorization header, empty when unauthenticated. */
export function getAccessToken(): string {
  return session?.tokens.accessToken ?? ''
}

/** Persist a completed authentication result as the active session. */
export function setSession(result: AuthResult): AuthSession {
  session = { user: result.user, tokens: result.tokens }
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
  notifySessionChange()
  return session
}

/** Drop the local session without contacting the backend. */
export function clearSession(): void {
  session = null
  localStorage.removeItem(SESSION_STORAGE_KEY)
  notifySessionChange()
}

// ---------------------------------------------------------------------------
// Authentication calls
// ---------------------------------------------------------------------------

/** Phase one: verify credentials. May require choosing a tenant identity. */
export async function requestLogin(username: string, password: string): Promise<LoginResult> {
  const { data } = await authClient.post<LoginResult>('/auth/login', { username, password })
  if (data.auth) {
    setSession(data.auth)
  }
  return data
}

/** Create a new tenant with its owning administrator and sign in. */
export async function requestRegister(
  username: string,
  password: string,
  tenantName: string
): Promise<AuthResult> {
  const { data } = await authClient.post<AuthResult>('/auth/register', {
    username,
    password,
    tenantName,
  })
  setSession(data)
  return data
}

/**
 * Phase two: activate a tenant identity.
 *
 * `preAuthToken` is used right after login; when omitted the current access
 * token authorizes the switch (in-app identity switching).
 */
export async function requestSelectTenant(
  tenantId: string,
  preAuthToken?: string
): Promise<AuthResult> {
  const headers = preAuthToken ? undefined : authorizationHeader()
  const { data } = await authClient.post<AuthResult>(
    '/auth/select-tenant',
    { tenantId, preAuthToken },
    { headers }
  )
  setSession(data)
  return data
}

/**
 * Rotate the refresh token and replace the stored session.
 *
 * Concurrent callers share one in-flight request. Returns null when the
 * session could not be renewed, in which case it has been cleared.
 */
export function refreshSession(): Promise<AuthSession | null> {
  if (refreshPromise) {
    return refreshPromise
  }
  const refreshToken = session?.tokens.refreshToken
  if (!refreshToken) {
    clearSession()
    return Promise.resolve(null)
  }

  refreshPromise = authClient
    .post<AuthResult>('/auth/refresh', { refreshToken })
    .then(({ data }) => setSession(data))
    .catch((error) => {
      console.error('[auth] Session refresh failed', error)
      clearSession()
      return null
    })
    .finally(() => {
      refreshPromise = null
    })

  return refreshPromise
}

/** Revoke the refresh token server-side, then clear the local session. */
export async function requestLogout(): Promise<void> {
  const refreshToken = session?.tokens.refreshToken
  if (refreshToken) {
    await authClient
      .post('/auth/logout', { refreshToken }, { headers: authorizationHeader() })
      .catch((error) => {
        // The local session is dropped regardless of the network outcome.
        console.error('[auth] Logout request failed', error)
      })
  }
  clearSession()
}

function authorizationHeader(): Record<string, string> | undefined {
  const token = getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : undefined
}
