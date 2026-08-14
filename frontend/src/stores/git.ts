/**
 * Git store - manages Git panel state, authorization, and operations.
 * Implements the three-state Git panel: auth → init → detail.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import api from '@/utils/api'
import { getSession } from '@/utils/authSession'
import { i18n } from '@/i18n'
import { extractApiMessage, notifyError } from '@/utils/notifyError'

/**
 * Owner of the current session. The gateway re-validates and overwrites this
 * value from the access token, so it is only a convenience for the payload.
 */
function currentUserId(): string {
  return getSession()?.user.userId ?? ''
}

/**
 * Report a failed Git operation: keep the parsed reason on the store for
 * inline rendering and raise a toast so the user never has to read the logs.
 * Remote rejections are localized from the backend's reason code, so the
 * message names the exact credential setting to fix.
 */
function reportGitError(error: unknown, fallbackKey: string): string {
  const fallback = i18n.global.t(fallbackKey) as string
  const message = extractApiMessage(error, fallback)
  notifyError(error, fallback)
  return message
}

export interface FileChange {
  path: string
  /**
   * Raw Git porcelain letter: `M` modified, `A` added, `D` deleted,
   * `R` renamed, `C` copied, `T` type-changed, `?` untracked, `U` conflict.
   * Mapped to colours/badges by `@/utils/gitDecoration`.
   */
  status: string
  staged: boolean
  /** Source path of a rename/copy, empty otherwise. */
  originalPath?: string
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

  /** Local branches, current one first. */
  const localBranches = computed(() =>
    branches.value.filter((b) => !b.is_remote)
  )

  /** Remote-tracking branches (``origin/...``), excluding the symbolic HEAD. */
  const remoteBranches = computed(() =>
    branches.value.filter((b) => b.is_remote && !b.name.endsWith('/HEAD'))
  )

  /** Local branch names, used to tell which remote branches are not local yet. */
  const localBranchNames = computed(
    () => new Set(localBranches.value.map((b) => b.name))
  )

  /** Whether a remote ref (``origin/dev``) already has a local counterpart. */
  function hasLocalCounterpart(remoteRef: string): boolean {
    return localBranchNames.value.has(remoteRef.split('/').slice(1).join('/'))
  }

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
        originalPath: c.original_path || '',
      }))
    } catch (e) {
      error.value = extractApiMessage(e, i18n.global.t('git.statusFailed') as string)
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
    } catch (e) {
      error.value = reportGitError(e, 'git.initFailed')
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
        user_id: currentUserId(),
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
    } catch (e) {
      error.value = reportGitError(e, 'git.publishFailed')
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
    } catch (e) {
      error.value = reportGitError(e, 'git.commitFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Push to remote. Defaults to the checked-out branch; pass ``branch`` to
   * publish another local branch without switching to it first.
   */
  async function push(branch?: string): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/push', {
        workspace_path: ws.path,
        user_id: currentUserId(),
        branch: branch || '',
      })
      await refreshRepoState()
    } catch (e) {
      error.value = reportGitError(e, 'git.pushFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Pull from remote into the checked-out branch (or ``branch`` when given).
   */
  async function pull(branch?: string): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/pull', {
        workspace_path: ws.path,
        user_id: currentUserId(),
        branch: branch || '',
      })
      await refreshRepoState()
    } catch (e) {
      error.value = reportGitError(e, 'git.pullFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Update remote-tracking refs. Cheap and non-destructive: it only refreshes
   * what the remote looks like, so ahead/behind and the remote branch list stop
   * being stale without touching the working tree.
   */
  async function fetchFromRemote(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/fetch', {
        workspace_path: ws.path,
        user_id: currentUserId(),
      })
      await refreshRepoState()
    } catch (e) {
      error.value = reportGitError(e, 'git.fetchFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Materialize a remote branch locally, tracking its upstream.
   * ``remoteRef`` is the full remote name, e.g. "origin/dev".
   */
  async function checkoutRemoteBranch(remoteRef: string): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    const localName = remoteRef.split('/').slice(1).join('/')
    if (!localName) return

    isLoading.value = true
    try {
      await api.post('/git/checkout', {
        workspace_path: ws.path,
        branch: localName,
        create: true,
        start_point: remoteRef,
        track: true,
      })
      currentBranch.value = localName
      await refreshRepoState()
      await fetchCommits(localName)
    } catch (e) {
      error.value = reportGitError(e, 'git.checkoutFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Delete a local branch. Without ``force`` Git refuses to drop unmerged work.
   */
  async function deleteBranch(branch: string, force = false): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/branch/delete', {
        workspace_path: ws.path,
        branch,
        force,
      })
      await refreshRepoState()
    } catch (e) {
      error.value = reportGitError(e, 'git.deleteBranchFailed')
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Move the current branch back to ``commitHash``.
   * ``hard`` discards working-tree changes, so callers must confirm it first.
   */
  async function resetTo(
    commitHash: string,
    mode: 'soft' | 'mixed' | 'hard' = 'mixed'
  ): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    isLoading.value = true
    try {
      await api.post('/git/reset', {
        workspace_path: ws.path,
        commit_hash: commitHash,
        mode,
      })
      await refreshRepoState()
      await fetchCommits(currentBranch.value)
    } catch (e) {
      error.value = reportGitError(e, 'git.resetFailed')
    } finally {
      isLoading.value = false
    }
  }

  /** Stage every change in the working tree. */
  async function stageAll(): Promise<void> {
    await stageFiles([])
  }

  /** Remove every change from the index. */
  async function unstageAll(): Promise<void> {
    await unstageFiles([])
  }

  /** Reload the state that any branch-level operation can invalidate. */
  async function refreshRepoState(): Promise<void> {
    await fetchStatus()
    await fetchBranches()
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

    isLoading.value = true
    try {
      await api.post('/git/checkout', { workspace_path: ws.path, branch, create })
      currentBranch.value = branch
      await refreshRepoState()
      // Refresh commit history for the newly checked-out branch
      await fetchCommits(branch)
    } catch (e) {
      error.value = reportGitError(e, 'git.checkoutFailed')
    } finally {
      isLoading.value = false
    }
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
    localBranches,
    remoteBranches,
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
    stageAll,
    unstageAll,
    commit,
    push,
    pull,
    fetchFromRemote,
    fetchBranches,
    checkout,
    checkoutRemoteBranch,
    deleteBranch,
    resetTo,
    hasLocalCounterpart,
    refreshRepoState,
    fetchCommits,
    selectCommit,
    fetchCommitFileDiff,
    reset,
  }
})
