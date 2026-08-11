/**
 * Workspace store - manages workspace creation, selection, listing, and Git binding.
 * Workspace = a SQLMesh project directory on disk, optionally backed by a remote Git repo.
 * All operations are backed by the backend API via Gateway.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import api from '@/utils/api'
import { notifyError } from '@/utils/notifyError'

export interface WorkspaceInfo {
  id: string
  name: string
  path: string          // Absolute path on disk
  status: string
  gitRepoUrl: string    // Remote Git repo URL (empty = no remote)
  createdAt: string     // UTC ISO 8601
}

import type { GitCredential, CreateGitCredentialRequest, UpdateGitCredentialRequest } from '@/types/git-credentials'

export interface GitCredentialInfo {
  hasCredential: boolean
  provider: string
  serverUrl: string
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<WorkspaceInfo[]>([])
  const currentWorkspaceId = ref<string | null>(null)
  const isLoading = ref(false)
  const gitCredential = ref<GitCredentialInfo | null>(null)

  // Multi-credential state
  const gitCredentials = ref<GitCredential[]>([])
  const isLoadingCredentials = ref(false)

  const currentWorkspace = computed(() =>
    workspaces.value.find((ws) => ws.id === currentWorkspaceId.value) || null
  )

  // Restore selected workspace ID from localStorage
  const storedId = localStorage.getItem('current_workspace_id')
  if (storedId) {
    currentWorkspaceId.value = storedId
  }

  function _persistSelection() {
    if (currentWorkspaceId.value) {
      localStorage.setItem('current_workspace_id', currentWorkspaceId.value)
    } else {
      localStorage.removeItem('current_workspace_id')
    }
  }

  /**
   * Fetch workspaces from backend.
   */
  async function fetchWorkspaces(): Promise<void> {
    const authStore = useAuthStore()
    const userId = authStore.user?.userId || 'user-admin-001'

    isLoading.value = true
    try {
      const { data } = await api.get('/workspaces', { params: { user_id: userId } })
      workspaces.value = (data.workspaces || []).map((ws: any) => ({
        id: ws.workspace_id,
        name: ws.name,
        path: ws.path,
        status: ws.status || 'active',
        gitRepoUrl: ws.git_repo_url || '',
        createdAt: ws.created_at,
      }))

      // Drop a stale persisted selection if the workspace no longer exists.
      if (
        currentWorkspaceId.value &&
        !workspaces.value.some((ws) => ws.id === currentWorkspaceId.value)
      ) {
        currentWorkspaceId.value = workspaces.value[0]?.id || null
        _persistSelection()
      }

      // Auto-select first if none selected
      if (!currentWorkspaceId.value && workspaces.value.length > 0) {
        currentWorkspaceId.value = workspaces.value[0].id
        _persistSelection()
      }
    } catch (e) {
      notifyError(e, 'Failed to fetch workspaces')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Create a new workspace via backend API.
   */
  async function createWorkspace(
    name: string,
    dialect = 'duckdb',
    gitRepoUrl = ''
  ): Promise<WorkspaceInfo> {
    const authStore = useAuthStore()
    const userId = authStore.user?.userId || 'user-admin-001'

    const { data } = await api.post('/workspaces', {
      user_id: userId,
      name,
      dialect,
      git_repo_url: gitRepoUrl,
    })

    const ws: WorkspaceInfo = {
      id: data.workspace_id,
      name: data.name,
      path: data.path,
      status: 'active',
      gitRepoUrl: data.git_repo_url || '',
      createdAt: data.created_at,
    }

    workspaces.value.push(ws)
    currentWorkspaceId.value = ws.id
    _persistSelection()
    return ws
  }

  /**
   * Bind a remote Git repository to an existing workspace.
   */
  async function bindGitRepo(workspaceId: string, gitRepoUrl: string): Promise<void> {
    const authStore = useAuthStore()
    const userId = authStore.user?.userId || 'user-admin-001'

    await api.post('/workspaces/bind-git', {
      workspace_id: workspaceId,
      user_id: userId,
      git_repo_url: gitRepoUrl,
    })

    // Update local state
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (ws) {
      ws.gitRepoUrl = gitRepoUrl
    }
  }

  /**
   * Sync workspace changes to remote Git.
   */
  async function syncToRemote(workspaceId: string, message = 'Auto-save'): Promise<void> {
    await api.post('/workspaces/sync', {
      workspace_id: workspaceId,
      message,
    })
  }

  /**
   * Delete a workspace.
   */
  async function deleteWorkspace(workspaceId: string, deleteRemote = false): Promise<void> {
    await api.delete(`/workspaces/${workspaceId}`, {
      params: { delete_remote: deleteRemote },
    })
    workspaces.value = workspaces.value.filter((ws) => ws.id !== workspaceId)
    if (currentWorkspaceId.value === workspaceId) {
      currentWorkspaceId.value = workspaces.value[0]?.id || null
    }
    _persistSelection()
  }

  /**
   * Switch to a workspace.
   */
  function selectWorkspace(id: string) {
    currentWorkspaceId.value = id
    _persistSelection()
  }

  /**
   * Remove workspace from local list (client-side only).
   */
  function removeWorkspace(id: string) {
    workspaces.value = workspaces.value.filter((ws) => ws.id !== id)
    if (currentWorkspaceId.value === id) {
      currentWorkspaceId.value = workspaces.value[0]?.id || null
    }
    _persistSelection()
  }

  // ---- Git Credentials (Multi-credential) ----

  /**
   * Fetch all Git credentials for the current user.
   */
  async function fetchGitCredentials(): Promise<void> {
    isLoadingCredentials.value = true
    try {
      const { data } = await api.get('/git/credentials')
      gitCredentials.value = data.credentials || []
      // Update legacy single-credential state for backward compatibility
      if (gitCredentials.value.length > 0) {
        const defaultCred = gitCredentials.value.find(c => c.is_default) || gitCredentials.value[0]
        gitCredential.value = {
          hasCredential: true,
          provider: defaultCred.provider,
          serverUrl: defaultCred.server_url || '',
        }
      } else {
        gitCredential.value = { hasCredential: false, provider: '', serverUrl: '' }
      }
    } catch {
      gitCredentials.value = []
      gitCredential.value = null
    } finally {
      isLoadingCredentials.value = false
    }
  }

  /**
   * Legacy: Check if current user has Git credentials (delegates to multi-credential fetch).
   */
  async function fetchGitCredential(): Promise<void> {
    await fetchGitCredentials()
  }

  /**
   * Create a new Git credential.
   */
  async function createGitCredential(req: CreateGitCredentialRequest): Promise<void> {
    await api.post('/git/credentials', req)
    await fetchGitCredentials()
  }

  /**
   * Update an existing Git credential.
   */
  async function updateGitCredential(credentialId: string, req: UpdateGitCredentialRequest): Promise<void> {
    await api.put(`/git/credentials/${credentialId}`, req)
    await fetchGitCredentials()
  }

  /**
   * Delete a Git credential.
   */
  async function deleteGitCredential(credentialId?: string): Promise<void> {
    if (credentialId) {
      await api.delete(`/git/credentials/${credentialId}`)
    }
    await fetchGitCredentials()
  }

  /**
   * Set a credential as default.
   */
  async function setDefaultGitCredential(credentialId: string): Promise<void> {
    await api.patch(`/git/credentials/${credentialId}/default`)
    await fetchGitCredentials()
  }

  /**
   * Legacy: Save Git credential (for backward compatibility with GitAuthPanel).
   */
  async function saveGitCredential(
    provider: string,
    token: string,
    serverUrl = ''
  ): Promise<void> {
    await createGitCredential({
      provider: provider as any,
      auth_type: 'pat',
      token,
      server_url: serverUrl,
    })
  }

  return {
    workspaces,
    currentWorkspaceId,
    currentWorkspace,
    isLoading,
    gitCredential,
    gitCredentials,
    isLoadingCredentials,
    fetchWorkspaces,
    createWorkspace,
    bindGitRepo,
    syncToRemote,
    deleteWorkspace,
    selectWorkspace,
    removeWorkspace,
    fetchGitCredential,
    fetchGitCredentials,
    createGitCredential,
    updateGitCredential,
    deleteGitCredential,
    setDefaultGitCredential,
    saveGitCredential,
  }
})
