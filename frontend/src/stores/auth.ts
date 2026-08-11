/**
 * Auth store - manages user authentication state.
 * MVP: hardcoded admin/admin123, no backend call needed.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface UserInfo {
  userId: string
  username: string
  role: string
}

// Hardcoded credentials for MVP
const MVP_USERS: Record<string, { password: string; info: UserInfo }> = {
  admin: {
    password: 'admin123',
    info: {
      userId: 'user-admin-001',
      username: 'admin',
      role: 'admin',
    },
  },
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  const isAuthenticated = computed(() => user.value !== null)

  // Restore session from localStorage on init
  const stored = localStorage.getItem('auth_user')
  if (stored) {
    try {
      user.value = JSON.parse(stored)
    } catch {
      localStorage.removeItem('auth_user')
    }
  }

  /**
   * Login with username and password.
   * MVP: validates against hardcoded credentials.
   */
  async function login(username: string, password: string): Promise<boolean> {
    const entry = MVP_USERS[username]
    if (!entry || entry.password !== password) {
      return false
    }

    user.value = entry.info
    localStorage.setItem('auth_user', JSON.stringify(entry.info))
    return true
  }

  /**
   * Logout and clear session.
   */
  function logout() {
    user.value = null
    localStorage.removeItem('auth_user')
  }

  return {
    user,
    isAuthenticated,
    login,
    logout,
  }
})
