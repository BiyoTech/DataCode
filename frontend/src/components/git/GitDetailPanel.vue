<script setup lang="ts">
/**
 * GitDetailPanel - IntelliJ-style horizontal Git panel (State C).
 * Layout: [Branches | Commit history | Changed-files tree]
 * - Left pane: branch tree + create-branch input.
 * - Center pane: commit history of the current branch.
 * - Right pane: directory tree of files changed in the selected commit.
 * Designed for bottom panel placement.
 */
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useGitStore, type CommitFile, type CommitInfo } from '@/stores/git'
import { useFileStore } from '@/stores/files'
import { formatLocalTime } from '@/utils/timezone'
import { baseName, kindOfStatus, letterOfStatus } from '@/utils/gitDecoration'
import IconButton from '@/components/common/IconButton.vue'

const { t } = useI18n()
const gitStore = useGitStore()
const fileStore = useFileStore()

// UI state
const newBranchName = ref('')
// Collapsed directory paths in the changed-files tree.
const collapsedDirs = ref<Set<string>>(new Set())

const syncLabel = computed(() => {
  const parts: string[] = []
  if (gitStore.ahead > 0) parts.push(`↑${gitStore.ahead}`)
  if (gitStore.behind > 0) parts.push(`↓${gitStore.behind}`)
  return parts.length > 0 ? parts.join(' ') : ''
})

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

async function handleCreateBranch() {
  if (!newBranchName.value.trim()) return
  await gitStore.checkout(newBranchName.value.trim(), true)
  newBranchName.value = ''
}

async function handleSwitchBranch(branchName: string) {
  if (branchName === gitStore.currentBranch) return
  await gitStore.checkout(branchName)
}

// ---- Branch-level Git operations ----

/** Local name a remote ref maps to: "origin/dev" → "dev". */
function localNameOf(remoteRef: string): string {
  return remoteRef.split('/').slice(1).join('/')
}

/** Delete confirmation target; null when the dialog is closed. */
const deleteTarget = ref<string | null>(null)
const forceDelete = ref(false)

function openDeleteBranch(branchName: string) {
  deleteTarget.value = branchName
  forceDelete.value = false
}

async function confirmDeleteBranch() {
  const branchName = deleteTarget.value
  if (!branchName) return
  deleteTarget.value = null
  await gitStore.deleteBranch(branchName, forceDelete.value)
}

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
      <div class="pane-header">
        <span class="pane-title" :title="gitStore.currentBranch">{{ gitStore.currentBranch }}</span>
        <span v-if="syncLabel" class="sync-badge">{{ syncLabel }}</span>
        <div class="pane-actions">
          <IconButton
            class="pane-action"
            :label="t('git.fetch')"
            :disabled="gitStore.isLoading"
            @click="gitStore.fetchFromRemote()"
          ><t-icon name="download" size="14px" /></IconButton>
          <IconButton
            class="pane-action"
            :label="t('git.pullCurrent')"
            :disabled="gitStore.isLoading"
            @click="gitStore.pull()"
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

      <!-- Branch tree -->
      <div class="branch-tree">
        <!-- Local branches -->
        <div class="branch-section">
          <div class="branch-section-title">{{ t('git.localBranches') }}</div>
          <div
            v-for="branch in gitStore.localBranches"
            :key="branch.name"
            class="branch-node"
            :class="{ current: branch.name === gitStore.currentBranch }"
            :title="t('git.switchBranch')"
            @click="handleSwitchBranch(branch.name)"
          >
            <span class="branch-leaf">🌿</span>
            <span class="branch-label">{{ branch.name }}</span>
            <div class="branch-actions">
              <IconButton
                class="pane-action"
                :label="t('git.pushBranch')"
                :disabled="gitStore.isLoading"
                @click.stop="gitStore.push(branch.name)"
              ><t-icon name="arrow-up" size="13px" /></IconButton>
              <IconButton
                v-if="branch.name !== gitStore.currentBranch"
                class="pane-action danger"
                :label="t('git.deleteBranch')"
                :disabled="gitStore.isLoading"
                @click.stop="openDeleteBranch(branch.name)"
              ><t-icon name="delete" size="13px" /></IconButton>
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
            :title="
              gitStore.hasLocalCounterpart(branch.name)
                ? t('git.alreadyLocal')
                : t('git.checkoutRemote')
            "
            @click="
              gitStore.hasLocalCounterpart(branch.name)
                ? handleSwitchBranch(localNameOf(branch.name))
                : gitStore.checkoutRemoteBranch(branch.name)
            "
          >
            <span class="branch-leaf">☁</span>
            <span class="branch-label">{{ branch.name }}</span>
            <div class="branch-actions">
              <IconButton
                v-if="!gitStore.hasLocalCounterpart(branch.name)"
                class="pane-action"
                :label="t('git.checkoutRemote')"
                :disabled="gitStore.isLoading"
                @click.stop="gitStore.checkoutRemoteBranch(branch.name)"
              ><t-icon name="download" size="13px" /></IconButton>
            </div>
          </div>
        </div>
      </div>

      <!-- Create branch input -->
      <div class="create-branch">
        <input
          v-model="newBranchName"
          class="branch-input"
          :placeholder="t('git.newBranchPlaceholder')"
          @keydown.enter="handleCreateBranch"
        />
      </div>
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

    <!-- Delete branch dialog -->
    <Teleport to="body">
      <div v-if="deleteTarget" class="dialog-overlay" @click.self="deleteTarget = null">
        <div class="dialog">
          <h4 class="dialog-title">
            {{ t('git.confirmDeleteBranch', { branch: deleteTarget }) }}
          </h4>
          <p class="dialog-text">{{ t('git.forceDeleteBranchHint') }}</p>

          <label class="dialog-option">
            <input type="checkbox" v-model="forceDelete" />
            <span>{{ t('git.forceDelete') }}</span>
          </label>

          <div class="dialog-actions">
            <button class="btn-cancel" @click="deleteTarget = null">
              {{ t('common.cancel') }}
            </button>
            <button class="btn-primary danger" @click="confirmDeleteBranch">
              {{ t('common.delete') }}
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

.create-branch {
  padding: 6px 8px;
  border-top: 1px solid var(--border);
}

.branch-input {
  width: 100%;
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
  color: var(--text-primary);
  font-size: 11px;
  outline: none;
}

.branch-input:focus {
  border-color: var(--accent);
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
