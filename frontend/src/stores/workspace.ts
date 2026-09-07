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
import { useQuotaStore } from './quotas'

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
  /**
   * Environment opened by default. A branch is no longer a workspace attribute:
   * it belongs to an environment, which owns it 1:1.
   */
  defaultEnv: string
  /** Name of this workspace's production environment (usually `prod`). */
  productionEnv: string
  /** Branch of the environment currently open; empty until opened. */
  currentBranch: string
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

/**
 * Map an API workspace payload onto the store model.
 *
 * `currentBranch` is not part of the listing: it only becomes known once an
 * environment has been opened, so it is carried over from the previous state.
 */
function toWorkspaceInfo(ws: any, path = '', currentBranch = ''): WorkspaceInfo {
  return {
    id: ws.workspace_id,
    name: ws.name,
    path,
    status: ws.status || 'active',
    dialect: ws.dialect || 'duckdb',
    gitRepoUrl: ws.git_repo_url || '',
    defaultEnv: ws.default_env || 'prod',
    productionEnv: ws.production_env || 'prod',
    currentBranch,
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

  // ---- Environment memory ----
  //
  // The last environment opened per workspace, so a page refresh returns the
  // user to the development line they were working on instead of the workspace
  // default. This is a RESTORE HINT only: the current environment is always
  // derived from the working-copy path, never from this value — a stale hint
  // can at worst cost one rejected open before the default is used.

  const ENV_MEMORY_PREFIX = 'workspace_environment:'

  function _rememberEnvironment(workspaceId: string, environment: string): void {
    if (!workspaceId || !environment) return
    try {
      localStorage.setItem(ENV_MEMORY_PREFIX + workspaceId, environment)
    } catch {
      // Storage may be unavailable (private mode); memory is best-effort.
    }
  }

  function _forgetEnvironment(workspaceId: string): void {
    try {
      localStorage.removeItem(ENV_MEMORY_PREFIX + workspaceId)
    } catch {
      // Ignore: nothing to clean up when storage is unavailable.
    }
  }

  /** Last environment opened in a workspace, or '' when unknown. */
  function rememberedEnvironment(workspaceId: string): string {
    if (!workspaceId) return ''
    try {
      return localStorage.getItem(ENV_MEMORY_PREFIX + workspaceId) || ''
    } catch {
      return ''
    }
  }

  /**
   * Fetch workspaces the caller may access, then restore/auto-select one.
   *
   * A workspace is only selected after `openWorkspace` succeeds, so the local
   * working copy always exists before any file/plan/git call uses its path.
   * The restore reopens the workspace's LAST USED environment (see
   * `rememberedEnvironment`); when that environment is gone — destroyed or
   * archived since — the memory is dropped and the workspace default opens.
   */
  async function fetchWorkspaces(): Promise<void> {
    isLoading.value = true
    try {
      const { data } = await api.get('/workspaces')
      // The listing carries no working-copy state, so what the caller already
      // materialized is preserved instead of being dropped on every refresh.
      const previous = new Map(
        workspaces.value.map((ws) => [ws.id, { path: ws.path, branch: ws.currentBranch }])
      )
      workspaces.value = (data.workspaces || []).map((ws: any) => {
        const local = previous.get(ws.workspace_id)
        return toWorkspaceInfo(ws, local?.path || '', local?.branch || '')
      })

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
        const remembered = rememberedEnvironment(candidate.id)
        if (remembered) {
          try {
            await openWorkspace(candidate.id, remembered)
          } catch (e: any) {
            // Keep the memory for transport failures so a later boot can still
            // restore the line; only a definitive refusal (the environment no
            // longer exists or cannot be opened) discards it.
            const status = e?.response?.status
            if (status !== 404 && status !== 409) throw e
            _forgetEnvironment(candidate.id)
            await openWorkspace(candidate.id)
          }
        } else {
          await openWorkspace(candidate.id)
        }
      }
    } catch (e) {
      notifyError(e, 'Failed to fetch workspaces')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Materialize the caller's working copy of one ENVIRONMENT and make the
   * workspace current. Passing no environment adopts the workspace default.
   *
   * Rejects locally when the backend already reported the workspace as not
   * openable.
   */
  async function openWorkspace(workspaceId: string, environment = ''): Promise<WorkspaceInfo> {
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (!ws) {
      throw new Error(`Unknown workspace: ${workspaceId}`)
    }

    isOpening.value = true
    try {
      const { data } = await api.post(`/workspaces/${workspaceId}/open`, { environment })
      ws.path = data.workspace_path
      ws.currentBranch = data.branch || ''
      currentWorkspaceId.value = ws.id
      // Remember which environment was opened so a refresh restores it. The
      // backend echoes the resolved name (the request may have been empty).
      _rememberEnvironment(ws.id, data.environment || environment)
      _persistSelection()
      return ws
    } finally {
      isOpening.value = false
    }
  }

  /**
   * Adopt the working copy returned by an environment switch.
   *
   * Switching environment changes WHICH worktree is active, not which workspace,
   * so only the path and the derived branch move. The environment itself is
   * never stored here as state: it is derived from the path by the environment
   * store, which keeps the two from ever disagreeing. It IS remembered (restore
   * hint only) so the next page refresh reopens the same line.
   */
  function applyOpenedEnvironment(
    workspacePath: string,
    branch: string,
    environment = ''
  ): void {
    const ws = currentWorkspace.value
    if (!ws || !workspacePath) return
    ws.path = workspacePath
    ws.currentBranch = branch || ws.currentBranch
    if (environment) _rememberEnvironment(ws.id, environment)
  }

  /**
   * List the environments a workspace offers, so the user can pick one before
   * opening it. Read from the platform registry: it is the same list for every
   * member, unlike the branches a member happens to hold locally.
   */
  async function fetchOpenableEnvironments(
    workspaceId: string
  ): Promise<{ environmentId: string; name: string; envType: string; status: string }[]> {
    const { data } = await api.get(`/workspaces/${workspaceId}/environments`)
    return (data?.environments || [])
      .filter((env: any) => env.status !== 'archived')
      .map((env: any) => ({
        environmentId: env.environment_id,
        name: env.name,
        envType: env.env_type,
        status: env.status,
      }))
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
    try {
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
    } catch (err) {
      // A quota rejection must refresh the cached overview, so the panel the
      // user opens next shows the server's numbers rather than a stale budget.
      if ((err as { response?: { data?: { error_code?: string } } })?.response
        ?.data?.error_code === 'QUOTA_EXCEEDED') {
        void useQuotaStore().refresh()
      }
      throw err
    }
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
   * Commit and push one environment's working copy to the remote.
   *
   * The environment must be passed explicitly: each environment is a separate
   * worktree, so omitting it would push the workspace default rather than what
   * the user is looking at.
   */
  async function syncToRemote(
    workspaceId: string,
    environment: string,
    message = 'Auto-save'
  ): Promise<void> {
    await api.post('/workspaces/sync', {
      workspace_id: workspaceId,
      environment,
      message,
    })
  }

  /**
   * Delete a workspace (owner only).
   *
   * Deletion is complete and irreversible on the platform side: rows, leases,
   * working copies, derived data. The remote Git repository is always preserved,
   * which is why this takes no options — there is nothing left to choose.
   */
  async function deleteWorkspace(workspaceId: string): Promise<void> {
    await api.delete(`/workspaces/${workspaceId}`)
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
   * Switch to a workspace by opening one of its environments first.
   */
  async function selectWorkspace(id: string, environment = ''): Promise<void> {
    if (id === currentWorkspaceId.value && !environment && currentWorkspace.value?.path) {
      return
    }
    await openWorkspace(id, environment)
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
    _forgetEnvironment(id)
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
    applyOpenedEnvironment,
    rememberedEnvironment,
    fetchOpenableEnvironments,
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
