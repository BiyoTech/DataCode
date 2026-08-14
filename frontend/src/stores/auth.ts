/**
 * Auth store - reactive facade over the authentication session.
 *
 * Identity and tenant are separate concepts: one account may own its tenant
 * (primary account) and belong to other tenants (sub account). The active
 * tenant is chosen at login time and can be switched in-app.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import api from '@/utils/api'
import {
  clearSession,
  getSession,
  onSessionChange,
  requestLogin,
  requestLogout,
  requestRegister,
  requestSelectTenant,
  type AuthSession,
  type LoginResult,
  type MembershipInfo,
  type TenantRole,
  type UserInfo,
} from '@/utils/authSession'

export type { MembershipInfo, TenantRole, UserInfo }

/** A member of the active tenant, as shown in the admin member list. */
export interface TenantMember {
  userId: string
  username: string
  role: TenantRole
  isOwner: boolean
  /** Join timestamp in UTC ISO 8601; convert for display. */
  joinedAt: string
}

/** Pending identity selection state between login phase one and two. */
export interface PendingSelection {
  preAuthToken: string
  memberships: MembershipInfo[]
}

export const useAuthStore = defineStore('auth', () => {
  const initial = getSession()
  const user = ref<UserInfo | null>(initial?.user ?? null)
  const pendingSelection = ref<PendingSelection | null>(null)

  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const requiresTenantSelection = computed(() => pendingSelection.value !== null)

  // Keep the store in sync with refreshes and switches performed elsewhere
  // (e.g. the axios interceptor rotating tokens).
  onSessionChange((session: AuthSession | null) => {
    user.value = session?.user ?? null
  })

  /**
   * Phase one of login. When the account has several tenant identities the
   * caller must follow up with `selectTenant`.
   */
  async function login(username: string, password: string): Promise<LoginResult> {
    const result = await requestLogin(username, password)
    if (result.requiresTenantSelection) {
      pendingSelection.value = {
        preAuthToken: result.preAuthToken ?? '',
        memberships: result.memberships ?? [],
      }
    } else {
      pendingSelection.value = null
    }
    return result
  }

  /** Register a new tenant and sign in as its administrator. */
  async function register(
    username: string,
    password: string,
    tenantName: string
  ): Promise<UserInfo> {
    const result = await requestRegister(username, password, tenantName)
    pendingSelection.value = null
    return result.user
  }

  /**
   * Activate a tenant identity. Uses the pending pre-auth token right after
   * login, or the current access token when switching inside the app.
   */
  async function selectTenant(tenantId: string): Promise<UserInfo> {
    const preAuthToken = pendingSelection.value?.preAuthToken
    const result = await requestSelectTenant(tenantId, preAuthToken)
    pendingSelection.value = null
    return result.user
  }

  /** Abandon a pending identity selection (e.g. user navigates back). */
  function cancelTenantSelection(): void {
    pendingSelection.value = null
    clearSession()
  }

  /** List every tenant identity of the signed-in account. */
  async function listMemberships(): Promise<MembershipInfo[]> {
    const { data } = await api.get<{ memberships: MembershipInfo[] }>('/auth/memberships')
    return data.memberships ?? []
  }

  /** Revoke the session server-side and clear local state. */
  async function logout(): Promise<void> {
    await requestLogout()
    pendingSelection.value = null
  }

  /**
   * Change the current account's password. All sessions are revoked by the
   * backend, so the caller must send the user back to the login screen.
   */
  async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await api.post('/auth/change-password', { currentPassword, newPassword })
    clearSession()
    pendingSelection.value = null
  }

  /** List members of the active tenant (admin only). */
  async function listMembers(): Promise<TenantMember[]> {
    const { data } = await api.get<{ members: TenantMember[] }>('/auth/members')
    return data.members ?? []
  }

  /**
   * Add a sub account to the active tenant (admin only).
   *
   * An existing username is linked with its own credentials; an unknown one
   * creates a new account with the given initial password.
   */
  async function addMember(payload: {
    username: string
    password: string
    role: TenantRole
  }): Promise<{ member: TenantMember; userCreated: boolean }> {
    const { data } = await api.post<{ member: TenantMember; userCreated: boolean }>(
      '/auth/members',
      payload
    )
    return data
  }

  return {
    user,
    pendingSelection,
    isAuthenticated,
    isAdmin,
    requiresTenantSelection,
    login,
    register,
    selectTenant,
    cancelTenantSelection,
    listMemberships,
    logout,
    changePassword,
    listMembers,
    addMember,
  }
})
