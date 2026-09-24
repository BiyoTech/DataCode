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
  /**
   * Environment this branch backs, or null for a branch outside the platform's
   * `env/` namespace (e.g. brought in from an external repository).
   */
  environment: string | null
  /** Whether the platform owns the branch's lifecycle. */
  managed: boolean
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

/** One file that differs between two branches. */
export interface BranchDiffFile {
  path: string
  status: string
  original_path?: string
}

/** A commit a merge would bring in (no email: the preview list stays compact). */
export interface MergeCommit {
  hash: string
  short_hash: string
  author_name: string
  date: string // ISO 8601; rendered in the user's timezone
  message: string
}

/**
 * Everything needed to confirm a branch merge, including the conflict verdict
 * from git's own merge machinery — so the dialog states whether the merge can
 * land instead of guessing from changed paths.
 */
export interface MergePreview {
  source_branch: string
  target_branch: string
  source_ref: string
  target_ref: string
  /** False when remote-tracking refs could not be refreshed (no credential). */
  remote_synced: boolean
  merge_base: string
  commits: MergeCommit[]
  commits_ahead: number
  files: BranchDiffFile[]
  conflicts: string[]
  can_merge: boolean
  up_to_date: boolean
  /** Commits the source branch holds locally but has not pushed yet. */
  source_unpushed: number
}

/** Outcome of merging a base branch into the current working copy. */
export interface SyncResult {
  branch: string
  base_branch: string
  conflicted: boolean
  conflicts: string[]
  merge_commit: string
  up_to_date: boolean
}

/**
 * How the backend integrated the remote branch. `conflicted` is a SUCCESSFUL
 * outcome: the conflicted files are in the working tree waiting to be resolved.
 */
export type PullOutcome = 'up_to_date' | 'fast_forward' | 'merged' | 'conflicted'

/** Outcome of pulling the remote branch into this working copy. */
export interface PullResult {
  branch: string
  outcome: PullOutcome
  conflicted: boolean
  conflicts: string[]
  commit: string
  integrated_commits: number
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
  /**
   * Environment the checked-out branch backs, derived by the backend from the
   * branch name. Empty for a branch the platform does not manage.
   */
  const currentEnvironment = ref('')
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

  // ---- Branch merge (code promotion, step 1 of 2) ----
  //
  // Promotion is split into two explicit user actions: merge the code here,
  // then plan & apply + reload the code location on the target environment.
  // A conflict cannot be automated away, so it is surfaced instead of hidden.
  const mergePreview = ref<MergePreview | null>(null)
  const isPreviewLoading = ref(false)
  const isMerging = ref(false)
  const isSyncing = ref(false)
  /** Conflicted paths of the last refused merge or of a conflicted sync. */
  const mergeConflicts = ref<string[]>([])

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
      // user_id lets the backend resolve this user's git credential for the
      // commit's author identity — same wiring as push/pull/fetch.
      await api.post('/git/commit', {
        workspace_path: ws.path,
        message: msg,
        user_id: currentUserId(),
      })
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
   *
   * The backend picks the integration strategy (fast-forward, else a real merge
   * commit), so a divergent environment branch pulls cleanly instead of failing
   * on git's "specify how to reconcile" refusal. A CONFLICTED result is
   * returned as a success: the conflicted files are now in the working tree and
   * are listed in `mergeConflicts`, exactly like after a sync. Returns `null`
   * when the pull was rejected (no workspace, dirty working copy, remote error).
   */
  async function pull(branch?: string): Promise<PullResult | null> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return null

    isLoading.value = true
    error.value = null
    mergeConflicts.value = []
    try {
      const { data } = await api.post('/git/pull', {
        workspace_path: ws.path,
        user_id: currentUserId(),
        branch: branch || '',
      })
      const result = data as PullResult
      mergeConflicts.value = result.conflicts || []
      await refreshRepoState()
      return result
    } catch (e) {
      error.value = reportGitError(e, 'git.pullFailed')
      return null
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
   * Fetch branches, each annotated with the environment it backs.
   *
   * Read-only: branches are created, switched and deleted through the
   * environment registry, never here.
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
    currentEnvironment.value = data.current_environment || ''
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

  /**
   * Load the pre-flight preview of merging `source` into `target`.
   * Read-only: it refreshes remote-tracking refs but never the working tree.
   */
  async function loadMergePreview(
    source: string,
    target: string
  ): Promise<MergePreview | null> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return null

    isPreviewLoading.value = true
    mergePreview.value = null
    mergeConflicts.value = []
    try {
      const { data } = await api.get('/git/branch-diff', {
        params: {
          workspace_path: ws.path,
          source,
          target,
        },
      })
      mergePreview.value = data as MergePreview
      return mergePreview.value
    } catch (e) {
      error.value = reportGitError(e, 'git.mergePreviewFailed')
      return null
    } finally {
      isPreviewLoading.value = false
    }
  }

  /** Fetch one file's patch between two branches (merge-base diff). */
  async function fetchBranchFileDiff(
    source: string,
    target: string,
    filePath: string
  ): Promise<string> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return ''

    try {
      const { data } = await api.get('/git/branch-diff', {
        params: {
          workspace_path: ws.path,
          source,
          target,
          file_path: filePath,
        },
      })
      return data.diff || ''
    } catch (e) {
      error.value = reportGitError(e, 'git.mergePreviewFailed')
      return ''
    }
  }

  /**
   * Merge `source` into `target` and publish the result on the remote.
   *
   * No working copy is modified: the target environment picks the merge up with
   * its own explicit pull + reload. A conflict is reported back as the list of
   * conflicted paths, since it can only be resolved in a working copy.
   */
  async function mergeBranches(
    source: string,
    target: string,
    message = ''
  ): Promise<boolean> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return false

    isMerging.value = true
    mergeConflicts.value = []
    try {
      await api.post('/git/merge', {
        workspace_path: ws.path,
        user_id: currentUserId(),
        source_branch: source,
        target_branch: target,
        message,
      })
      await refreshRepoState()
      return true
    } catch (e: any) {
      // The backend inlines the conflicted paths at the top level of the error
      // body, so the panel can list them without parsing a message.
      mergeConflicts.value = e?.response?.data?.conflicts ?? []
      error.value = reportGitError(e, 'git.mergeFailed')
      return false
    } finally {
      isMerging.value = false
    }
  }

  /**
   * Merge `base` into the branch of the current working copy.
   *
   * This is where a merge conflict is meant to be resolved: the conflicted
   * files stay in the working tree and appear as `U` entries, so the user fixes
   * them in the editor and commits.
   */
  async function syncWithBase(base: string, message = ''): Promise<SyncResult | null> {
    const workspaceStore = useWorkspaceStore()
    const ws = workspaceStore.currentWorkspace
    if (!ws) return null

    isSyncing.value = true
    mergeConflicts.value = []
    try {
      const { data } = await api.post('/git/sync', {
        workspace_path: ws.path,
        user_id: currentUserId(),
        base_branch: base,
        message,
      })
      const result = data as SyncResult
      mergeConflicts.value = result.conflicts || []
      await refreshRepoState()
      return result
    } catch (e) {
      error.value = reportGitError(e, 'git.syncFailed')
      return null
    } finally {
      isSyncing.value = false
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
    currentEnvironment.value = ''
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
    mergePreview.value = null
    isPreviewLoading.value = false
    isMerging.value = false
    isSyncing.value = false
    mergeConflicts.value = []
  }

  return {
    // State
    panelState,
    isDetailExpanded,
    isLoading,
    error,
    isInitialized,
    currentBranch,
    currentEnvironment,
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
    mergePreview,
    isPreviewLoading,
    isMerging,
    isSyncing,
    mergeConflicts,
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
    resetTo,
    hasLocalCounterpart,
    refreshRepoState,
    fetchCommits,
    selectCommit,
    fetchCommitFileDiff,
    loadMergePreview,
    fetchBranchFileDiff,
    mergeBranches,
    syncWithBase,
    reset,
  }
})
