/**
 * Git store - manages Git panel state, authorization, and operations.
 * Implements the three-state Git panel: auth → init → detail.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import api from '@/utils/api'

export interface FileChange {
  path: string
  status: 'M' | 'A' | 'D' | 'R'
  staged: boolean
}

export interface BranchInfo {
  name: string
  is_remote: boolean
  last_commit_hash: string
  last_commit_message: string
}

export interface CommitInfo {
  hash: string
  short_hash: string
  author_name: string
  author_email: string
  date: string // ISO 8601 string (with offset); UI converts to user local time
  message: string
}

export interface CommitFile {
  path: string
  status: string // M | A | D | R ...
}

export type GitPanelState = 'auth' | 'init' | 'detail'

export const useGitStore = defineStore('git', () => {
  // Panel state
  const panelState = ref<GitPanelState>('auth')
  const isDetailExpanded = ref(true)
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  // Git status
  const isInitialized = ref(false)
  const currentBranch = ref('main')
  const remoteUrl = ref<string | null>(null)
  const ahead = ref(0)
  const behind = ref(0)
  const changes = ref<FileChange[]>([])
  const isClean = computed(() => changes.value.length === 0)

  // Branches
  const branches = ref<BranchInfo[]>([])

  // Commit history (for selected branch) + selected commit's changed files
  const commits = ref<CommitInfo[]>([])
  const isLoadingCommits = ref(false)
  const selectedCommitHash = ref<string | null>(null)
  const commitFiles = ref<CommitFile[]>([])
  const isLoadingCommitFiles = ref(false)

  // Commit message input
  const commitMessage = ref('')

  /**
   * Determine panel state based on credentials + git init status.
   */
  async function refreshPanelState(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    isLoading.value = true
    error.value = null

    try {
      // Check if user has git credentials
      await workspaceStore.fetchGitCredential()
      const hasCred = workspaceStore.gitCredential?.hasCredential ?? false

      if (!hasCred) {
        panelState.value = 'auth'
        return
      }

      // Check if current workspace has git initialized
      const ws = workspaceStore.currentWorkspace
      if (!ws) {
        panelState.value = 'auth'
        return
      }

      const { data } = await api.get('/git/init-status', {
        params: { workspace_path: ws.path },
      })

      isInitialized.value = data.initialized

      if (!data.initialized) {
        panelState.value = 'init'
      } else {
        panelState.value = 'detail'
        await fetchStatus()
      }
    } catch (e: any) {
      error.value = e.message || 'Failed to check Git state'
      panelState.value = 'auth'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Fetch git status (branch, changes, ahead/behind).
   */
  async function fetchStatus(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    try {
      const { data } = await api.get('/git/status', {
        params: { workspace_path: ws.path },
      })
      currentBranch.value = data.branch || 'main'
      remoteUrl.value = data.remote_url || null
      ahead.value = data.ahead || 0
      behind.value = data.behind || 0
      changes.value = (data.changes || []).map((c: any) => ({
        path: c.path,
        status: c.status,
        staged: c.staged,
      }))
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch Git status'
    }
  }

  /**
   * Initialize git in current workspace.
   */
  async function initRepo(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/init', { workspace_path: ws.path })
      isInitialized.value = true
      panelState.value = 'detail'
      await fetchStatus()
    } catch (e: any) {
      error.value = e.message || 'Failed to initialize Git'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Publish: init + create remote repo + push.
   */
  async function publish(repoName: string, provider: string, isPrivate = true): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      const { data } = await api.post('/git/publish', {
        workspace_path: ws.path,
        user_id: 'user-admin-001',
        provider,
        repo_name: repoName,
        is_private: isPrivate,
      })
      if (data.success) {
        remoteUrl.value = data.remote_url
        isInitialized.value = true
        panelState.value = 'detail'
        await fetchStatus()
      }
    } catch (e: any) {
      error.value = e.message || 'Failed to publish'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Stage files.
   */
  async function stageFiles(paths: string[] = []): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    await api.post('/git/stage', { workspace_path: ws.path, paths })
    await fetchStatus()
  }

  /**
   * Unstage files.
   */
  async function unstageFiles(paths: string[] = []): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    await api.post('/git/unstage', { workspace_path: ws.path, paths })
    await fetchStatus()
  }

  /**
   * Commit staged changes.
   */
  async function commit(message?: string): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    const msg = message || commitMessage.value
    if (!msg.trim()) return

    isLoading.value = true
    try {
      await api.post('/git/commit', { workspace_path: ws.path, message: msg })
      commitMessage.value = ''
      await fetchStatus()
    } catch (e: any) {
      error.value = e.message || 'Commit failed'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Push to remote.
   */
  async function push(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/push', { workspace_path: ws.path, user_id: 'user-admin-001' })
      await fetchStatus()
    } catch (e: any) {
      error.value = e.message || 'Push failed'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Pull from remote.
   */
  async function pull(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/pull', { workspace_path: ws.path, user_id: 'user-admin-001' })
      await fetchStatus()
    } catch (e: any) {
      error.value = e.message || 'Pull failed'
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Fetch branches.
   */
  async function fetchBranches(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    const { data } = await api.get('/git/branches', {
      params: { workspace_path: ws.path },
    })
    branches.value = data.branches || []
    currentBranch.value = data.current_branch || 'main'
  }

  /**
   * Checkout branch.
   */
  async function checkout(branch: string, create = false): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    await api.post('/git/checkout', { workspace_path: ws.path, branch, create })
    currentBranch.value = branch
    await fetchStatus()
    // Refresh commit history for the newly checked-out branch
    await fetchCommits(branch)
  }

  /**
   * Fetch commit history for a branch (defaults to current branch).
   * Resets the selected commit and its file list.
   */
  async function fetchCommits(branch?: string, limit = 50): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoadingCommits.value = true
    try {
      const { data } = await api.get('/git/log', {
        params: {
          workspace_path: ws.path,
          branch: branch || currentBranch.value,
          limit,
        },
      })
      commits.value = data.commits || []
      // Auto-select the latest commit so the file tree has content
      selectedCommitHash.value = null
      commitFiles.value = []
      if (commits.value.length > 0) {
        await selectCommit(commits.value[0].hash)
      }
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch commit history'
    } finally {
      isLoadingCommits.value = false
    }
  }

  /**
   * Select a commit and load its changed files into the file tree.
   */
  async function selectCommit(commitHash: string): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    selectedCommitHash.value = commitHash
    isLoadingCommitFiles.value = true
    try {
      const { data } = await api.get('/git/commit-files', {
        params: { workspace_path: ws.path, commit_hash: commitHash },
      })
      commitFiles.value = data.files || []
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch commit files'
      commitFiles.value = []
    } finally {
      isLoadingCommitFiles.value = false
    }
  }

  /**
   * Fetch the patch diff for one file in a commit.
   */
  async function fetchCommitFileDiff(commitHash: string, filePath: string): Promise<string> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return ''

    try {
      const { data } = await api.get('/git/commit-file-diff', {
        params: { workspace_path: ws.path, commit_hash: commitHash, file_path: filePath },
      })
      return data.diff || ''
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch commit file diff'
      return ''
    }
  }

  /** Clear workspace-scoped git state (used on workspace switch). */
  function reset() {
    panelState.value = 'auth'
    isDetailExpanded.value = true
    isLoading.value = false
    error.value = null
    isInitialized.value = false
    currentBranch.value = 'main'
    remoteUrl.value = null
    ahead.value = 0
    behind.value = 0
    changes.value = []
    branches.value = []
    commits.value = []
    isLoadingCommits.value = false
    selectedCommitHash.value = null
    commitFiles.value = []
    isLoadingCommitFiles.value = false
    commitMessage.value = ''
  }

  return {
    // State
    panelState,
    isDetailExpanded,
    isLoading,
    error,
    isInitialized,
    currentBranch,
    remoteUrl,
    ahead,
    behind,
    changes,
    isClean,
    branches,
    commitMessage,
    commits,
    isLoadingCommits,
    selectedCommitHash,
    commitFiles,
    isLoadingCommitFiles,
    // Actions
    refreshPanelState,
    fetchStatus,
    initRepo,
    publish,
    stageFiles,
    unstageFiles,
    commit,
    push,
    pull,
    fetchBranches,
    checkout,
    fetchCommits,
    selectCommit,
    fetchCommitFileDiff,
    reset,
  }
})
