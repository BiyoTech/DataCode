/**
 * Workspace store - manages workspace listing, opening, membership and Git binding.
 *
 * A workspace is a TENANT-level shared unit whose only durable code carrier is
 * Git. Each user gets a private local working copy, materialized on demand by
 * `openWorkspace`. Therefore `path` is NOT returned by the list endpoint: it is
 * filled in once the workspace has been opened, and a workspace only becomes
 * `currentWorkspace` after a successful open — every consumer of
 * `currentWorkspace.path` can rely on it being ready.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { notifyError } from '@/utils/notifyError'

/** Reason codes returned by the backend when a workspace cannot be opened. */
export type OpenBlockedReason = '' | 'no_git_repo'

export interface WorkspaceInfo {
  id: string
  name: string
  /**
   * Local working-copy path of the CURRENT user. Empty until the workspace has
   * been opened; guaranteed non-empty for `currentWorkspace`.
   */
  path: string
  status: string
  dialect: string
  gitRepoUrl: string    // Remote Git repo URL (empty = no remote)
  gitBranch: string     // Branch checked out / default branch
  role: string          // Caller's role inside this workspace
  isOwner: boolean
  /**
   * Effective permission codes of the caller inside this workspace, resolved by
   * the gateway. Used for UI gating only; the backend re-checks every call.
   */
  permissions: string[]
  openable: boolean     // Backend-computed; never re-derived in the UI
  openBlockedReason: OpenBlockedReason
  createdAt: string     // UTC ISO 8601
}

export interface WorkspaceMember {
  userId: string
  username: string
  role: string
  isOwner: boolean
  joinedAt: string      // UTC ISO 8601
}

export interface TenantMemberCandidate {
  userId: string
  username: string
}

import type { GitCredential, CreateGitCredentialRequest, UpdateGitCredentialRequest } from '@/types/git-credentials'

export interface GitCredentialInfo {
  hasCredential: boolean
  provider: string
  serverUrl: string
}

/** Map an API workspace payload onto the store model. */
function toWorkspaceInfo(ws: any, path = ''): WorkspaceInfo {
  return {
    id: ws.workspace_id,
    name: ws.name,
    path,
    status: ws.status || 'active',
    dialect: ws.dialect || 'duckdb',
    gitRepoUrl: ws.git_repo_url || '',
    gitBranch: ws.git_branch || 'main',
    role: ws.role || '',
    isOwner: Boolean(ws.is_owner),
    permissions: Array.isArray(ws.permissions) ? ws.permissions : [],
    openable: Boolean(ws.openable),
    openBlockedReason: (ws.open_blocked_reason || '') as OpenBlockedReason,
    createdAt: ws.created_at,
  }
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<WorkspaceInfo[]>([])
  const currentWorkspaceId = ref<string | null>(null)
  const isLoading = ref(false)
  const isOpening = ref(false)
  const gitCredential = ref<GitCredentialInfo | null>(null)

  // Multi-credential state
  const gitCredentials = ref<GitCredential[]>([])
  const isLoadingCredentials = ref(false)

  const currentWorkspace = computed(() =>
    workspaces.value.find((ws) => ws.id === currentWorkspaceId.value) || null
  )

  // Restore selected workspace ID from localStorage
  const storedId = localStorage.getItem('current_workspace_id')

  function _persistSelection() {
    if (currentWorkspaceId.value) {
      localStorage.setItem('current_workspace_id', currentWorkspaceId.value)
    } else {
      localStorage.removeItem('current_workspace_id')
    }
  }

  /**
   * Fetch workspaces the caller may access, then restore/auto-select one.
   *
   * A workspace is only selected after `openWorkspace` succeeds, so the local
   * working copy always exists before any file/plan/git call uses its path.
   */
  async function fetchWorkspaces(): Promise<void> {
    isLoading.value = true
    try {
      const { data } = await api.get('/workspaces')
      const previousPaths = new Map(workspaces.value.map((ws) => [ws.id, ws.path]))
      workspaces.value = (data.workspaces || []).map((ws: any) =>
        toWorkspaceInfo(ws, previousPaths.get(ws.workspace_id) || '')
      )

      const desiredId =
        (currentWorkspaceId.value ?? storedId) || null
      const candidate =
        workspaces.value.find((ws) => ws.id === desiredId && ws.openable) ||
        workspaces.value.find((ws) => ws.openable) ||
        null

      if (!candidate) {
        currentWorkspaceId.value = null
        _persistSelection()
        return
      }
      if (candidate.id !== currentWorkspaceId.value || !candidate.path) {
        await openWorkspace(candidate.id)
      }
    } catch (e) {
      notifyError(e, 'Failed to fetch workspaces')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Materialize the caller's working copy (clone or fetch + checkout) and make
   * the workspace current. Rejects locally when the backend already reported the
   * workspace as not openable.
   */
  async function openWorkspace(workspaceId: string, branch = ''): Promise<WorkspaceInfo> {
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (!ws) {
      throw new Error(`Unknown workspace: ${workspaceId}`)
    }

    isOpening.value = true
    try {
      const { data } = await api.post(`/workspaces/${workspaceId}/open`, { branch })
      ws.path = data.workspace_path
      ws.gitBranch = data.branch || ws.gitBranch
      currentWorkspaceId.value = ws.id
      _persistSelection()
      return ws
    } finally {
      isOpening.value = false
    }
  }

  /** List remote branches so the user can pick one before opening. */
  async function fetchRemoteBranches(workspaceId: string): Promise<string[]> {
    const { data } = await api.get(`/workspaces/${workspaceId}/remote-branches`)
    return data.branches || []
  }

  /**
   * Create a new workspace. Ownership and tenant come from the verified
   * identity injected by the gateway.
   */
  async function createWorkspace(
    name: string,
    dialect = 'duckdb',
    gitRepoUrl = ''
  ): Promise<WorkspaceInfo> {
    const { data } = await api.post('/workspaces', {
      name,
      dialect,
      git_repo_url: gitRepoUrl,
    })

    const ws = toWorkspaceInfo(data)
    workspaces.value.push(ws)
    // The creator's local scaffold already exists; open it to obtain its path.
    await openWorkspace(ws.id)
    return ws
  }

  /**
   * Bind a remote Git repository to an existing workspace (owner only).
   * Once bound, every member of the workspace can open it.
   */
  async function bindGitRepo(workspaceId: string, gitRepoUrl: string): Promise<void> {
    const { data } = await api.post('/workspaces/bind-git', {
      workspace_id: workspaceId,
      git_repo_url: gitRepoUrl,
    })

    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (ws) {
      ws.gitRepoUrl = data.git_repo_url || gitRepoUrl
      ws.openable = true
      ws.openBlockedReason = ''
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
   * Delete a workspace (owner only).
   */
  async function deleteWorkspace(workspaceId: string, deleteRemote = false): Promise<void> {
    await api.delete(`/workspaces/${workspaceId}`, {
      params: { delete_remote: deleteRemote },
    })
    removeWorkspace(workspaceId)
  }

  // ---- Membership (gateway-owned role bindings) ----

  /** List the members authorized to use a workspace. */
  async function listMembers(workspaceId: string): Promise<WorkspaceMember[]> {
    const { data } = await api.get(`/workspaces/${workspaceId}/members`)
    return (data.members || []).map((m: any) => ({
      userId: m.userId,
      username: m.username || m.userId,
      role: m.role,
      isOwner: Boolean(m.isOwner),
      joinedAt: m.joinedAt || '',
    }))
  }

  /** Grant a tenant member access to the workspace. */
  async function addMember(
    workspaceId: string,
    targetUserId: string,
    role: string
  ): Promise<void> {
    await api.post(`/workspaces/${workspaceId}/members`, {
      target_user_id: targetUserId,
      role,
    })
  }

  /** Revoke a member's access to the workspace. */
  async function removeMember(workspaceId: string, targetUserId: string): Promise<void> {
    await api.delete(`/workspaces/${workspaceId}/members/${targetUserId}`)
  }

  /** Tenant directory used to populate the member picker. */
  async function fetchTenantMemberCandidates(): Promise<TenantMemberCandidate[]> {
    const { data } = await api.get('/auth/tenant-members')
    return (data.candidates || []).map((c: any) => ({
      userId: c.userId,
      username: c.username,
    }))
  }

  /**
   * Switch to a workspace by opening the caller's working copy first.
   */
  async function selectWorkspace(id: string, branch = ''): Promise<void> {
    if (id === currentWorkspaceId.value && !branch && currentWorkspace.value?.path) {
      return
    }
    await openWorkspace(id, branch)
  }

  /**
   * Remove workspace from local list (client-side only).
   */
  function removeWorkspace(id: string) {
    workspaces.value = workspaces.value.filter((ws) => ws.id !== id)
    if (currentWorkspaceId.value === id) {
      currentWorkspaceId.value = null
      _persistSelection()
    }
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
      provider,
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
    isOpening,
    gitCredential,
    gitCredentials,
    isLoadingCredentials,
    fetchWorkspaces,
    openWorkspace,
    fetchRemoteBranches,
    createWorkspace,
    bindGitRepo,
    syncToRemote,
    deleteWorkspace,
    listMembers,
    addMember,
    removeMember,
    fetchTenantMemberCandidates,
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
