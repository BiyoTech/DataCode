<script setup lang="ts">
/**
 * GitDetailPanel - IntelliJ-style horizontal Git panel (State C).
 * Layout: [Branches | Commit history | Changed-files tree]
 * - Left pane: branch tree, merge / sync entry points.
 * - Center pane: commit history of the current branch.
 * - Right pane: directory tree of files changed in the selected commit.
 * Designed for bottom panel placement.
 *
 * This panel owns STEP 1 of promoting code between environments: the Git merge.
 * Step 2 (plan & apply on the target environment, then reload its code
 * location) lives on the Plan & Apply page. They are deliberately separate
 * actions: a merge conflict is an intermediate state only a human with a
 * working copy can resolve, so wrapping both steps in one "promote" button
 * would only hide that fact behind a black box.
 */
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useGitStore, type CommitFile, type CommitInfo } from '@/stores/git'
import { useEnvironmentStore } from '@/stores/environment'
import { useFileStore } from '@/stores/files'
import { useWipStore } from '@/stores/wip'
import { useNow } from '@/composables/useNow'
import { formatLocalTime, formatRelativeTime } from '@/utils/timezone'
import { baseName, kindOfStatus, letterOfStatus } from '@/utils/gitDecoration'
import IconButton from '@/components/common/IconButton.vue'

const { t } = useI18n()
const gitStore = useGitStore()
const environmentStore = useEnvironmentStore()
const fileStore = useFileStore()
const wipStore = useWipStore()

// UI state
// Collapsed directory paths in the changed-files tree.
const collapsedDirs = ref<Set<string>>(new Set())

const syncLabel = computed(() => {
  const parts: string[] = []
  if (gitStore.ahead > 0) parts.push(`↑${gitStore.ahead}`)
  if (gitStore.behind > 0) parts.push(`↓${gitStore.behind}`)
  return parts.length ? parts.join(' ') : ''
})

// ---- WIP durability contract (design 07 Q6) ----
// The indicator only matters while there is uncommitted work to lose; a clean
// working copy has nothing for the snapshot to protect.
const now = useNow()

const showWipIndicator = computed(
  () => wipStore.isEnabled && !gitStore.isClean,
)

/** True when the last protection is older than one snapshot interval. */
const wipIsStale = computed(() => {
  if (!wipStore.lastProtectedAt) return true
  const elapsedMs = now.value - Date.parse(wipStore.lastProtectedAt)
  return elapsedMs > wipStore.snapshotIntervalSeconds * 1000
})

const wipLabel = computed(() =>
  wipStore.lastProtectedAt
    ? t('git.wip.lastProtected', {
        time: formatRelativeTime(wipStore.lastProtectedAt),
      })
    : t('git.wip.neverProtected'),
)

/** Tooltip shows the exact local wall-clock time behind the relative label. */
const wipTooltip = computed(() =>
  wipStore.lastProtectedAt
    ? formatLocalTime(wipStore.lastProtectedAt, 'YYYY-MM-DD HH:mm:ss')
    : t('git.wip.neverProtectedTooltip'),
)

function reopenWipDialog(): void {
  wipStore.dialogVisible = true
}

/** One-letter badge and colour class, shared with the file tree decoration. */
function statusIcon(status: string): string {
  return letterOfStatus(status)
}

function statusClass(status: string): string {
  const kind = kindOfStatus(status)
  return kind ? `git-${kind}` : 'git-unknown'
}

function fileName(path: string): string {
  return baseName(path) || path
}

/** Format a commit date (ISO 8601 from backend) into the user's local time. */
function formatCommitDate(iso: string): string {
  if (!iso) return ''
  return formatLocalTime(iso, 'YYYY-MM-DD HH:mm')
}

// ---- Changed-files directory tree ----

interface TreeNode {
  name: string
  path: string
  isDir: boolean
  status?: string
  children: Map<string, TreeNode>
}

interface TreeRow {
  name: string
  path: string
  depth: number
  isDir: boolean
  status?: string
  fileCount: number
}

/** Build a nested directory tree from the flat list of changed files. */
function buildTree(files: CommitFile[]): TreeNode {
  const root: TreeNode = { name: '', path: '', isDir: true, children: new Map() }
  for (const file of files) {
    const parts = file.path.split('/')
    let node = root
    parts.forEach((part, idx) => {
      const isLast = idx === parts.length - 1
      let child = node.children.get(part)
      if (!child) {
        child = {
          name: part,
          path: parts.slice(0, idx + 1).join('/'),
          isDir: !isLast,
          status: isLast ? file.status : undefined,
          children: new Map(),
        }
        node.children.set(part, child)
      }
      node = child
    })
  }
  return root
}

/** Count leaf files under a directory node (for the "N files" badge). */
function countFiles(node: TreeNode): number {
  if (!node.isDir) return 1
  let total = 0
  for (const child of node.children.values()) total += countFiles(child)
  return total
}

/** Flatten the tree into ordered, indented rows; respects collapsed dirs. */
function flattenTree(node: TreeNode, depth: number, rows: TreeRow[]): void {
  // Directories first, then files; each group alphabetically.
  const children = Array.from(node.children.values()).sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
    return a.name.localeCompare(b.name)
  })
  for (const child of children) {
    rows.push({
      name: child.name,
      path: child.path,
      depth,
      isDir: child.isDir,
      status: child.status,
      fileCount: child.isDir ? countFiles(child) : 0,
    })
    if (child.isDir && !collapsedDirs.value.has(child.path)) {
      flattenTree(child, depth + 1, rows)
    }
  }
}

const treeRows = computed<TreeRow[]>(() => {
  const rows: TreeRow[] = []
  flattenTree(buildTree(gitStore.commitFiles), 0, rows)
  return rows
})

function toggleDir(path: string) {
  const next = new Set(collapsedDirs.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  collapsedDirs.value = next
}

// ---- Actions ----

// ---- Branch-level Git operations ----

/** Reset dialog state. `hard` is destructive, so it is never the default. */
const resetTarget = ref<CommitInfo | null>(null)
const resetMode = ref<'soft' | 'mixed' | 'hard'>('mixed')

function openReset(commit: CommitInfo) {
  resetTarget.value = commit
  resetMode.value = 'mixed'
}

async function confirmReset() {
  const target = resetTarget.value
  if (!target) return
  const mode = resetMode.value
  resetTarget.value = null
  await gitStore.resetTo(target.hash, mode)
}

function handleSelectCommit(hash: string) {
  collapsedDirs.value = new Set()
  gitStore.selectCommit(hash)
}

async function handleTreeRowClick(row: TreeRow) {
  if (row.isDir) {
    toggleDir(row.path)
    return
  }

  const commitHash = gitStore.selectedCommitHash
  if (!commitHash) return

  const diff = await gitStore.fetchCommitFileDiff(commitHash, row.path)
  const commit = gitStore.commits.find((c) => c.hash === commitHash)
  const shortHash = commit?.short_hash || commitHash.slice(0, 8)
  fileStore.openVirtualFile({
    path: `git-diff://${commitHash}/${row.path}`,
    name: `${fileName(row.path)} (${shortHash}).diff`,
    content: diff,
    language: 'diff',
    readOnly: true,
  })
}

// ---- Branch merge / sync ----

/** Fallback production branch when the environment registry is not loaded yet. */
const DEFAULT_PRODUCTION_BRANCH = 'main'

/** Strip the remote prefix so `origin/env/dev` and `env/dev` are one branch. */
function plainBranchName(name: string, isRemote: boolean): string {
  return isRemote ? name.split('/').slice(1).join('/') : name
}

/**
 * Branch names the platform manages, deduplicated across local and remote refs.
 * Platform-external branches are excluded: they are read-only until adopted as
 * an environment, so they can take part in neither side of a merge.
 */
const mergeableBranches = computed<string[]>(() => {
  const names: string[] = []
  for (const branch of gitStore.branches) {
    if (!branch.managed) continue
    const name = plainBranchName(branch.name, branch.is_remote)
    if (name && !names.includes(name)) names.push(name)
  }
  return names
})

/** The workspace's production branch — the usual promotion target. */
const productionBranch = computed(
  () =>
    environmentStore.environments.find((env) => env.isProduction)?.branch ||
    DEFAULT_PRODUCTION_BRANCH
)

const mergeDialogOpen = ref(false)
const mergeSource = ref('')
const mergeTarget = ref('')
const mergeMessage = ref('')

const syncDialogOpen = ref(false)
const syncBase = ref('')

const preview = computed(() => gitStore.mergePreview)

const mergeTargetOptions = computed(() =>
  mergeableBranches.value.filter((name) => name !== mergeSource.value)
)

const syncBaseOptions = computed(() =>
  mergeableBranches.value.filter((name) => name !== gitStore.currentBranch)
)

/** Preview is only trustworthy while it describes the selected pair. */
const previewMatchesSelection = computed(
  () =>
    preview.value !== null &&
    preview.value.source_branch === mergeSource.value &&
    preview.value.target_branch === mergeTarget.value
)

function openMerge(source: string) {
  mergeSource.value = source
  const candidates = mergeableBranches.value.filter((name) => name !== source)
  mergeTarget.value = candidates.includes(productionBranch.value)
    ? productionBranch.value
    : candidates[0] || ''
  mergeMessage.value = ''
  mergeDialogOpen.value = true
  refreshPreview()
}

/** Load the pre-flight verdict for the selected branch pair. */
async function refreshPreview() {
  if (!mergeSource.value || !mergeTarget.value) return
  await gitStore.loadMergePreview(mergeSource.value, mergeTarget.value)
}

async function confirmMerge() {
  if (!previewMatchesSelection.value || !preview.value?.can_merge) return
  const merged = await gitStore.mergeBranches(
    mergeSource.value,
    mergeTarget.value,
    mergeMessage.value
  )
  if (merged) mergeDialogOpen.value = false
}

/** Open one file of the branch diff as a read-only editor tab. */
async function openBranchFileDiff(path: string) {
  const diff = await gitStore.fetchBranchFileDiff(
    mergeSource.value,
    mergeTarget.value,
    path
  )
  fileStore.openVirtualFile({
    path: `git-diff://${mergeTarget.value}...${mergeSource.value}/${path}`,
    name: `${fileName(path)} (${mergeSource.value} → ${mergeTarget.value}).diff`,
    content: diff,
    language: 'diff',
    readOnly: true,
  })
}

function openSync(base = '') {
  syncBase.value =
    base ||
    (syncBaseOptions.value.includes(productionBranch.value)
      ? productionBranch.value
      : syncBaseOptions.value[0] || '')
  syncDialogOpen.value = true
}

async function confirmSync() {
  if (!syncBase.value) return
  const result = await gitStore.syncWithBase(syncBase.value)
  if (!result) return
  syncDialogOpen.value = false
  // A conflicted sync is the expected path when a promotion was refused: the
  // conflicted files are now in the working tree, waiting to be resolved.
  if (!result.conflicted) await gitStore.fetchCommits()
}

/**
 * Pull the remote branch into this working copy.
 *
 * A divergent branch is integrated with a merge commit, so a pull can end
 * conflicted — that is a normal outcome, not an error: the conflicted paths are
 * listed by the panel's conflict bar and resolved in the editor. The history is
 * only refreshed when the merge actually completed.
 */
async function pullCurrent() {
  const result = await gitStore.pull()
  if (result && !result.conflicted) await gitStore.fetchCommits()
}

onMounted(async () => {
  await gitStore.fetchStatus()
  await gitStore.fetchBranches()
  // Load commit history for the current branch (auto-selects latest commit).
  await gitStore.fetchCommits()
})
</script>

<template>
  <div class="git-detail">
    <!-- Left: Branches panel -->
    <div class="branches-pane">
      <!-- WIP degradation banner: a dirty copy whose last snapshot FAILED.
           This is the "you are not protected" warning the durability contract
           (design 07 Q6) requires — without it the user assumes protection
           that does not exist. -->
      <div
        v-if="wipStore.isEnabled && !wipStore.isProtected"
        class="wip-banner error"
      >
        <t-icon name="error-circle" size="14px" />
        <span>{{ t('git.wip.unprotectedBanner') }}</span>
      </div>
      <!-- Pending snapshot whose dialog was deferred: protection stays paused
           until the user decides, so keep a non-blocking way back in. -->
      <div
        v-else-if="wipStore.pendingSnapshot && !wipStore.dialogVisible"
        class="wip-banner warn"
      >
        <t-icon name="time" size="14px" />
        <span>{{ t('git.wip.pendingBanner') }}</span>
        <button class="wip-banner-action" type="button" @click="reopenWipDialog">
          {{ t('git.wip.review') }}
        </button>
      </div>

      <div class="pane-header">
        <span class="pane-title" :title="gitStore.currentBranch">{{ gitStore.currentBranch }}</span>
        <span v-if="syncLabel" class="sync-badge">{{ syncLabel }}</span>
        <!-- WIP indicator: makes the protection window visible (design 07
             Q6). Grey while fresh; warning colour once it exceeds one
             snapshot interval. -->
        <span
          v-if="showWipIndicator"
          class="wip-badge"
          :class="{ stale: wipIsStale }"
          :title="wipTooltip"
        >
          <t-icon name="secured" size="12px" />
          {{ wipLabel }}
        </span>
        <div class="pane-actions">
          <IconButton
            class="pane-action"
            :label="t('git.fetch')"
            :disabled="gitStore.isLoading"
            @click="gitStore.fetchFromRemote()"
          ><t-icon name="download" size="14px" /></IconButton>
          <IconButton
            class="pane-action"
            :label="t('git.syncWithBase')"
            :disabled="gitStore.isLoading || gitStore.isSyncing || !syncBaseOptions.length"
            @click="openSync()"
          ><t-icon name="swap" size="14px" /></IconButton>
          <IconButton
            class="pane-action"
            :label="t('git.pullCurrent')"
            :disabled="gitStore.isLoading"
            @click="pullCurrent()"
          ><t-icon name="arrow-down" size="14px" /></IconButton>
          <IconButton
            class="pane-action"
            :label="t('git.pushCurrent')"
            :disabled="gitStore.isLoading"
            @click="gitStore.push()"
          ><t-icon name="arrow-up" size="14px" /></IconButton>
        </div>
      </div>

      <!-- Working-tree quick actions: keeps the panel self-sufficient so
           staging does not require switching to the sidebar. -->
      <div class="worktree-bar">
        <span class="worktree-label">
          {{
            gitStore.isClean
              ? t('git.noUncommitted')
              : t('git.uncommittedCount', { count: gitStore.changes.length })
          }}
        </span>
        <div class="worktree-actions">
          <IconButton
            class="pane-action"
            :label="t('git.stageAll')"
            :disabled="gitStore.isClean || gitStore.isLoading"
            @click="gitStore.stageAll()"
          ><t-icon name="add" size="14px" /></IconButton>
          <IconButton
            class="pane-action"
            :label="t('git.unstageAll')"
            :disabled="gitStore.isClean || gitStore.isLoading"
            @click="gitStore.unstageAll()"
          ><t-icon name="minus" size="14px" /></IconButton>
        </div>
      </div>

      <!-- Conflicted paths left by a refused merge or a conflicted sync. The
           resolution happens in the editor, so the panel only points at it. -->
      <div v-if="gitStore.mergeConflicts.length" class="conflict-bar">
        <div class="conflict-title">
          {{ t('git.conflictsTitle', { count: gitStore.mergeConflicts.length }) }}
        </div>
        <div v-for="path in gitStore.mergeConflicts" :key="path" class="conflict-file">
          {{ path }}
        </div>
        <div class="conflict-hint">{{ t('git.conflictsHint') }}</div>
      </div>

      <!-- Branch tree — lifecycle is read-only (a branch is an environment's
           code carrier), but merging between environments starts here. -->
      <div class="branch-tree">
        <!-- Local branches -->
        <div class="branch-section">
          <div class="branch-section-title">{{ t('git.localBranches') }}</div>
          <div
            v-for="branch in gitStore.localBranches"
            :key="branch.name"
            class="branch-node"
            :class="{ current: branch.name === gitStore.currentBranch }"
            :title="branch.managed ? t('git.branchOfEnvironment', { environment: branch.environment }) : t('git.unmanagedBranch')"
          >
            <span class="branch-leaf">🌿</span>
            <span class="branch-label">{{ branch.name }}</span>
            <span v-if="branch.managed" class="branch-env-tag">{{ branch.environment }}</span>
            <span v-else class="branch-env-tag unmanaged">{{ t('git.unmanaged') }}</span>
            <div class="branch-actions">
              <IconButton
                v-if="branch.managed && mergeableBranches.length > 1"
                class="pane-action"
                :label="t('git.mergeInto')"
                :disabled="gitStore.isLoading || gitStore.isMerging"
                @click.stop="openMerge(branch.name)"
              ><t-icon name="git-merge" size="13px" /></IconButton>
              <IconButton
                class="pane-action"
                :label="t('git.pushBranch')"
                :disabled="gitStore.isLoading"
                @click.stop="gitStore.push(branch.name)"
              ><t-icon name="arrow-up" size="13px" /></IconButton>
            </div>
          </div>
        </div>

        <!-- Remote branches -->
        <div class="branch-section">
          <div class="branch-section-title">{{ t('git.remoteBranches') }}</div>
          <div
            v-for="branch in gitStore.remoteBranches"
            :key="branch.name"
            class="branch-node remote"
            :class="{ linked: gitStore.hasLocalCounterpart(branch.name) }"
            :title="branch.managed ? t('git.branchOfEnvironment', { environment: branch.environment }) : t('git.unmanagedBranch')"
          >
            <span class="branch-leaf">☁</span>
            <span class="branch-label">{{ branch.name }}</span>
            <span v-if="branch.managed" class="branch-env-tag">{{ branch.environment }}</span>
            <div class="branch-actions">
              <IconButton
                v-if="branch.managed && mergeableBranches.length > 1"
                class="pane-action"
                :label="t('git.mergeInto')"
                :disabled="gitStore.isLoading || gitStore.isMerging"
                @click.stop="openMerge(plainBranchName(branch.name, true))"
              ><t-icon name="git-merge" size="13px" /></IconButton>
            </div>
          </div>
        </div>
      </div>

      <div class="branch-hint">{{ t('git.branchesManagedByEnvironment') }}</div>
    </div>

    <!-- Center: commit history -->
    <div class="changes-pane">
      <div v-if="gitStore.error" class="error-bar">{{ gitStore.error }}</div>

      <div class="changes-list">
        <div class="change-group history-group">
          <div class="group-header">
            <span class="group-title">{{ t('git.commitHistory') }}</span>
            <span class="group-count">{{ gitStore.commits.length }}</span>
          </div>

          <div v-if="gitStore.isLoadingCommits" class="hint-row">{{ t('common.loading') }}</div>
          <div v-else-if="gitStore.commits.length === 0" class="hint-row">{{ t('git.noCommits') }}</div>

          <div
            v-for="c in gitStore.commits"
            :key="c.hash"
            class="commit-item"
            :class="{ active: c.hash === gitStore.selectedCommitHash }"
            @click="handleSelectCommit(c.hash)"
          >
            <div class="commit-row">
              <span class="commit-msg" :title="c.message">{{ c.message }}</span>
              <IconButton
                class="pane-action commit-action"
                :label="t('git.resetToCommit')"
                :disabled="gitStore.isLoading"
                @click.stop="openReset(c)"
              ><t-icon name="rollback" size="13px" /></IconButton>
            </div>
            <span class="commit-meta">
              <span class="commit-author">{{ c.author_name }}</span>
              <span class="commit-hash">{{ c.short_hash }}</span>
              <span class="commit-date">{{ formatCommitDate(c.date) }}</span>
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- Right: Changed-files tree for the selected commit -->
    <div class="files-pane">
      <div class="pane-header">
        <span class="pane-title">{{ t('git.changedFiles') }}</span>
        <span v-if="gitStore.commitFiles.length > 0" class="sync-badge">
          {{ gitStore.commitFiles.length }}
        </span>
      </div>

      <div class="files-tree">
        <div v-if="gitStore.isLoadingCommitFiles" class="hint-row">{{ t('common.loading') }}</div>
        <div v-else-if="!gitStore.selectedCommitHash" class="hint-row">{{ t('git.selectCommitHint') }}</div>
        <div v-else-if="treeRows.length === 0" class="hint-row">{{ t('git.noFiles') }}</div>

        <div
          v-for="row in treeRows"
          :key="(row.isDir ? 'd-' : 'f-') + row.path"
          class="tree-row"
          :class="{ 'is-dir': row.isDir }"
          :style="{ paddingLeft: 8 + row.depth * 14 + 'px' }"
          @click="handleTreeRowClick(row)"
        >
          <template v-if="row.isDir">
            <span class="tree-caret">{{ collapsedDirs.has(row.path) ? '▸' : '▾' }}</span>
            <span class="tree-icon">📁</span>
            <span class="tree-name">{{ row.name }}</span>
            <span class="tree-count">{{ row.fileCount }}</span>
          </template>
          <template v-else>
            <span class="tree-caret-placeholder"></span>
            <span class="tree-status" :class="statusClass(row.status || '')">{{ statusIcon(row.status || '') }}</span>
            <span class="tree-name">{{ row.name }}</span>
          </template>
        </div>
      </div>
    </div>

    <!-- Merge dialog — step 1 of promoting code between environments. The
         conflict verdict comes from git's own merge machinery, so the user
         confirms knowing whether the merge can land at all. -->
    <Teleport to="body">
      <div v-if="mergeDialogOpen" class="dialog-overlay" @click.self="mergeDialogOpen = false">
        <div class="dialog dialog-wide">
          <h4 class="dialog-title">{{ t('git.mergeTitle') }}</h4>

          <div class="dialog-row">
            <label class="dialog-field">
              <span class="dialog-label">{{ t('git.mergeSource') }}</span>
              <select v-model="mergeSource" class="dialog-select" @change="refreshPreview">
                <option v-for="name in mergeableBranches" :key="name" :value="name">
                  {{ name }}
                </option>
              </select>
            </label>
            <span class="dialog-arrow">→</span>
            <label class="dialog-field">
              <span class="dialog-label">{{ t('git.mergeTarget') }}</span>
              <select v-model="mergeTarget" class="dialog-select" @change="refreshPreview">
                <option v-for="name in mergeTargetOptions" :key="name" :value="name">
                  {{ name }}
                </option>
              </select>
            </label>
          </div>

          <div v-if="gitStore.isPreviewLoading" class="dialog-text">
            {{ t('common.loading') }}
          </div>

          <template v-else-if="previewMatchesSelection && preview">
            <p v-if="preview.up_to_date" class="dialog-text">
              {{ t('git.mergeUpToDate', { target: mergeTarget, source: mergeSource }) }}
            </p>
            <p v-else class="dialog-text">
              {{
                t('git.mergeSummary', {
                  commits: preview.commits_ahead,
                  files: preview.files.length,
                })
              }}
            </p>

            <p v-if="!preview.remote_synced" class="dialog-warning">
              {{ t('git.mergeRemoteStale') }}
            </p>
            <p v-if="preview.source_unpushed > 0" class="dialog-warning">
              {{ t('git.mergeUnpushed', { count: preview.source_unpushed }) }}
            </p>
            <p v-if="preview.conflicts.length" class="dialog-warning">
              {{ t('git.mergeConflictWarning', { count: preview.conflicts.length }) }}
            </p>

            <!-- Changed files: clicking one opens the branch diff as a tab, the
                 same way a commit's files already do. -->
            <div v-if="preview.files.length" class="dialog-files">
              <div
                v-for="file in preview.files"
                :key="file.path"
                class="dialog-file"
                @click="openBranchFileDiff(file.path)"
              >
                <span class="tree-status" :class="statusClass(file.status)">
                  {{ statusIcon(file.status) }}
                </span>
                <span class="dialog-file-path">{{ file.path }}</span>
                <span
                  v-if="preview.conflicts.includes(file.path)"
                  class="dialog-file-conflict"
                >{{ t('git.conflictTag') }}</span>
              </div>
            </div>

            <label class="dialog-field">
              <span class="dialog-label">{{ t('git.mergeMessage') }}</span>
              <input
                v-model="mergeMessage"
                class="dialog-input"
                :placeholder="t('git.mergeMessagePlaceholder', {
                  source: mergeSource,
                  target: mergeTarget,
                })"
              />
            </label>

            <p class="dialog-hint">{{ t('git.mergeNextStepHint') }}</p>
          </template>

          <div class="dialog-actions">
            <button
              v-if="previewMatchesSelection && preview?.conflicts.length"
              class="btn-cancel"
              @click="mergeDialogOpen = false; openSync(mergeTarget)"
            >
              {{ t('git.resolveViaSync') }}
            </button>
            <button class="btn-cancel" @click="mergeDialogOpen = false">
              {{ t('common.cancel') }}
            </button>
            <button
              class="btn-primary"
              :disabled="
                gitStore.isMerging ||
                gitStore.isPreviewLoading ||
                !previewMatchesSelection ||
                !preview?.can_merge
              "
              @click="confirmMerge"
            >
              {{ gitStore.isMerging ? t('git.merging') : t('git.confirmMerge') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Sync dialog — brings the base branch INTO this working copy, which is
         where a conflict is resolved (editor + staging, no web merge tool). -->
    <Teleport to="body">
      <div v-if="syncDialogOpen" class="dialog-overlay" @click.self="syncDialogOpen = false">
        <div class="dialog">
          <h4 class="dialog-title">{{ t('git.syncTitle') }}</h4>
          <p class="dialog-text">
            {{ t('git.syncDescription', { branch: gitStore.currentBranch }) }}
          </p>

          <label class="dialog-field">
            <span class="dialog-label">{{ t('git.syncBase') }}</span>
            <select v-model="syncBase" class="dialog-select">
              <option v-for="name in syncBaseOptions" :key="name" :value="name">
                {{ name }}
              </option>
            </select>
          </label>

          <p v-if="!gitStore.isClean" class="dialog-warning">
            {{ t('git.syncDirtyWarning') }}
          </p>

          <div class="dialog-actions">
            <button class="btn-cancel" @click="syncDialogOpen = false">
              {{ t('common.cancel') }}
            </button>
            <button
              class="btn-primary"
              :disabled="gitStore.isSyncing || !syncBase || !gitStore.isClean"
              @click="confirmSync"
            >
              {{ gitStore.isSyncing ? t('git.syncing') : t('git.confirmSync') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Reset dialog: mode must be chosen explicitly because `hard` is
         irreversible for uncommitted work. -->
    <Teleport to="body">
      <div v-if="resetTarget" class="dialog-overlay" @click.self="resetTarget = null">
        <div class="dialog">
          <h4 class="dialog-title">{{ t('git.resetTitle') }}</h4>
          <p class="dialog-text">
            {{
              t('git.resetTargetHint', {
                branch: gitStore.currentBranch,
                commit: resetTarget.short_hash,
              })
            }}
          </p>
          <p class="dialog-commit">{{ resetTarget.message }}</p>

          <label class="dialog-option">
            <input type="radio" v-model="resetMode" value="soft" />
            <span>{{ t('git.resetModeSoft') }}</span>
          </label>
          <label class="dialog-option">
            <input type="radio" v-model="resetMode" value="mixed" />
            <span>{{ t('git.resetModeMixed') }}</span>
          </label>
          <label class="dialog-option">
            <input type="radio" v-model="resetMode" value="hard" />
            <span>{{ t('git.resetModeHard') }}</span>
          </label>

          <p v-if="resetMode === 'hard'" class="dialog-warning">
            {{ t('git.resetHardWarning') }}
          </p>

          <div class="dialog-actions">
            <button class="btn-cancel" @click="resetTarget = null">
              {{ t('common.cancel') }}
            </button>
            <button
              class="btn-primary"
              :class="{ danger: resetMode === 'hard' }"
              @click="confirmReset"
            >
              {{ t('git.confirmReset') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.git-detail {
  display: flex;
  flex: 1 1 auto;
  flex-direction: row;
  width: 100%;
  min-width: 0;
  height: 100%;
  overflow: hidden;
}

/* === Left: Branches pane === */
.branches-pane {
  flex: 0 0 228px;
  min-width: 200px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 6px 6px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  background: var(--bg-surface);
}

.pane-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sync-badge {
  font-size: 10px;
  background: var(--accent-dim);
  color: var(--accent);
  padding: 1px 5px;
  border-radius: 8px;
}

/* ---- WIP durability contract (design 07 Q6) ---- */
.wip-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}

.wip-badge.stale {
  color: var(--warning, #ed7b2f);
}

.wip-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.4;
  border-bottom: 1px solid var(--border);
}

.wip-banner.error {
  color: var(--error, #e34d59);
  background: var(--error-subtle, rgba(227, 77, 89, 0.06));
}

.wip-banner.warn {
  color: var(--warning, #ed7b2f);
}

.wip-banner-action {
  margin-left: auto;
  flex-shrink: 0;
  background: none;
  border: 1px solid currentColor;
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 11px;
  color: inherit;
  cursor: pointer;
  opacity: 0.85;
}

.wip-banner-action:hover {
  opacity: 1;
}

/* Header / row action buttons — revealed on hover to keep rows quiet. */
.pane-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
}

.pane-action {
  color: var(--text-muted);
}

.pane-action:hover:not(:disabled) {
  color: var(--accent);
}

.pane-action.danger:hover:not(:disabled) {
  color: var(--error);
}

/* Working-tree quick actions */
.worktree-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 4px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-muted);
}

.worktree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.worktree-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
}

/* Conflicted paths surfaced after a refused merge or a conflicted sync. */
.conflict-bar {
  padding: 6px 8px 6px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--error-subtle);
  font-size: 11px;
  color: var(--text-secondary);
  max-height: 132px;
  overflow-y: auto;
}

.conflict-title {
  font-weight: 600;
  color: var(--error);
  margin-bottom: 2px;
}

.conflict-file {
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conflict-hint {
  margin-top: 4px;
  color: var(--text-muted);
  line-height: 1.4;
}

.branch-tree {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.branch-section {
  margin-bottom: 4px;
}

.branch-section-title {
  padding: 4px 10px 2px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.branch-node {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 6px 3px 16px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.branch-node:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.branch-node.current {
  color: var(--accent);
  font-weight: 500;
}

.branch-node.remote {
  color: var(--text-muted);
}

/* A remote branch that already exists locally: switching, not fetching. */
.branch-node.remote.linked {
  color: var(--text-secondary);
}

.branch-leaf {
  font-size: 11px;
}

.branch-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Which environment a branch backs. Un-managed branches are read-only: they
   cannot be planned or applied until adopted as an environment. */
.branch-env-tag {
  flex-shrink: 0;
  font-size: 10px;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bg-hover, rgba(255, 255, 255, 0.08));
  color: var(--text-secondary);
}

.branch-env-tag.unmanaged {
  color: var(--text-muted);
  font-style: italic;
}

/* Row actions stay hidden until hover so the list reads as a branch list. */
.branch-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
  opacity: 0;
}

.branch-node:hover .branch-actions {
  opacity: 1;
}

/* Explains why the branch list has no create/switch/delete affordances. */
.branch-hint {
  padding: 6px 8px;
  border-top: 1px solid var(--border);
  font-size: 10px;
  line-height: 1.4;
  color: var(--text-muted);
}

/* === Center: Commit history pane === */
.changes-pane {
  flex: 1 1 0;
  min-width: 280px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.error-bar {
  padding: 4px 10px;
  background: var(--error-subtle);
  color: var(--error);
  font-size: 11px;
}

/* Changes list */
.changes-list {
  flex: 1;
  overflow-y: auto;
}

.change-group {
  margin-bottom: 2px;
}

.history-group {
  margin-top: 0;
}

.group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--bg-surface);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  position: sticky;
  top: 0;
  z-index: 1;
}

.group-count {
  background: var(--accent-dim);
  color: var(--text-secondary);
  padding: 0 5px;
  border-radius: 8px;
  font-size: 10px;
}

.group-action {
  margin-left: auto;
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 1px 5px;
  border-radius: 3px;
}

.group-action:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.change-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px 2px 16px;
  font-size: 12px;
  cursor: default;
}

.change-item:hover {
  background: var(--bg-hover);
}

.change-item:hover .change-action {
  opacity: 1;
}

.change-status {
  font-size: 11px;
  font-weight: 700;
  font-family: var(--font-mono);
  width: 12px;
  text-align: center;
}

.status-modified { color: var(--warning); }
.status-added { color: var(--success); }
.status-deleted { color: var(--error); }
.status-renamed { color: var(--accent); }
.status-unknown { color: var(--text-muted); }

/* Git decoration colours, shared with the file tree and the changes list. */
.git-untracked { color: var(--git-untracked); }
.git-added { color: var(--git-added); }
.git-modified { color: var(--git-modified); }
.git-renamed { color: var(--git-renamed); }
.git-deleted { color: var(--git-deleted); }
.git-conflict { color: var(--git-conflict); font-weight: 600; }
.git-unknown { color: var(--text-muted); }

.change-file {
  color: var(--text-primary);
  white-space: nowrap;
}

.change-dir {
  color: var(--text-muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.change-action {
  opacity: 0;
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 1px 4px;
  border-radius: 3px;
}

.change-action:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.clean-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 12px;
  color: var(--text-muted);
  font-size: 12px;
}

/* Commit history rows */
.hint-row {
  padding: 10px;
  text-align: center;
  color: var(--text-muted);
  font-size: 11px;
}

.commit-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 5px 10px 5px 16px;
  font-size: 12px;
  cursor: pointer;
  border-left: 2px solid transparent;
}

.commit-item:hover {
  background: var(--bg-hover);
}

.commit-item.active {
  background: var(--bg-hover);
  border-left-color: var(--accent);
}

.commit-msg {
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.commit-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.commit-row .commit-msg {
  flex: 1;
  min-width: 0;
}

.commit-action {
  opacity: 0;
  flex-shrink: 0;
}

.commit-item:hover .commit-action {
  opacity: 1;
}

/* === Dialogs (reset / delete branch) === */
.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}

.dialog {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 380px;
  max-width: 90vw;
  padding: 20px 22px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-float-lg);
}

.dialog-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

/* Merge dialog needs room for the branch pair and the changed-file list. */
.dialog-wide {
  min-width: 520px;
  max-width: 720px;
}

.dialog-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.dialog-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.dialog-label {
  font-size: 11px;
  color: var(--text-muted);
}

.dialog-arrow {
  padding-bottom: 6px;
  color: var(--text-muted);
}

.dialog-select,
.dialog-input {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text-primary);
  outline: none;
}

.dialog-select:focus,
.dialog-input:focus {
  border-color: var(--accent);
}

.dialog-files {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 0;
}

.dialog-file {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  font-size: 12px;
  cursor: pointer;
}

.dialog-file:hover {
  background: var(--bg-hover);
}

.dialog-file-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.dialog-file-conflict {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 10px;
  color: var(--error);
  text-transform: uppercase;
}

.dialog-hint {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-muted);
}

.dialog-text {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.dialog-commit {
  font-size: 12px;
  color: var(--text-muted);
  padding: 6px 8px;
  background: var(--bg-surface);
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dialog-option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: pointer;
}

.dialog-option input {
  accent-color: var(--accent);
}

.dialog-warning {
  font-size: 12px;
  color: var(--error);
  background: var(--error-subtle);
  padding: 6px 8px;
  border-radius: 4px;
  line-height: 1.5;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 4px;
}

.btn-cancel {
  padding: 7px 14px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.btn-cancel:hover {
  border-color: var(--text-muted);
}

.btn-primary {
  padding: 7px 14px;
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}

.btn-primary:hover {
  filter: brightness(1.1);
}

.btn-primary.danger {
  background: var(--error);
}

.commit-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: var(--text-muted);
}

.commit-author {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}

.commit-hash {
  font-family: var(--font-mono);
  color: var(--accent);
}

.commit-date {
  margin-left: auto;
  white-space: nowrap;
}

/* === Right: Changed-files tree === */
.files-pane {
  flex: 2 1 0;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.files-tree {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.tree-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.tree-row.is-dir {
  cursor: pointer;
  color: var(--text-primary);
}

.tree-row:not(.is-dir) {
  cursor: pointer;
}

.tree-row:hover {
  background: var(--bg-hover);
}

.tree-caret {
  width: 10px;
  font-size: 9px;
  color: var(--text-muted);
  text-align: center;
}

.tree-caret-placeholder {
  width: 10px;
}

.tree-icon {
  font-size: 11px;
}

.tree-status {
  font-size: 11px;
  font-weight: 700;
  font-family: var(--font-mono);
  width: 12px;
  text-align: center;
}

.tree-name {
  overflow: hidden;
  text-overflow: ellipsis;
}

.tree-count {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-muted);
  padding-left: 8px;
}
</style>
