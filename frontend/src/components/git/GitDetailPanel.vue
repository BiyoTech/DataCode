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
import { useGitStore, type CommitFile } from '@/stores/git'
import { useFileStore } from '@/stores/files'
import { formatLocalTime } from '@/utils/timezone'

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

function statusIcon(status: string): string {
  switch (status) {
    case 'M': return 'M'
    case 'A': return 'A'
    case 'D': return 'D'
    case 'R': return 'R'
    default: return '?'
  }
}

function statusClass(status: string): string {
  switch (status) {
    case 'M': return 'status-modified'
    case 'A': return 'status-added'
    case 'D': return 'status-deleted'
    case 'R': return 'status-renamed'
    default: return 'status-unknown'
  }
}

function fileName(path: string): string {
  return path.split('/').pop() || path
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
  await gitStore.checkout(branchName)
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
        <span class="pane-title">{{ gitStore.currentBranch }}</span>
        <span v-if="syncLabel" class="sync-badge">{{ syncLabel }}</span>
      </div>

      <!-- Branch tree -->
      <div class="branch-tree">
        <!-- Local branches -->
        <div class="branch-section">
          <div class="branch-section-title">Local</div>
          <div
            v-for="branch in gitStore.branches.filter(b => !b.is_remote)"
            :key="branch.name"
            class="branch-node"
            :class="{ current: branch.name === gitStore.currentBranch }"
            @click="handleSwitchBranch(branch.name)"
          >
            <span class="branch-leaf">🌿</span>
            <span class="branch-label">{{ branch.name }}</span>
          </div>
        </div>

        <!-- Remote branches -->
        <div class="branch-section">
          <div class="branch-section-title">Remote</div>
          <div
            v-for="branch in gitStore.branches.filter(b => b.is_remote)"
            :key="branch.name"
            class="branch-node remote"
          >
            <span class="branch-leaf">☁</span>
            <span class="branch-label">{{ branch.name }}</span>
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
            <span class="commit-msg" :title="c.message">{{ c.message }}</span>
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
  flex: 0 0 180px;
  min-width: 160px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  background: var(--bg-surface);
}

.sync-badge {
  font-size: 10px;
  background: var(--accent-dim);
  color: var(--accent);
  padding: 1px 5px;
  border-radius: 8px;
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
  padding: 3px 10px 3px 16px;
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
  cursor: default;
}

.branch-leaf {
  font-size: 11px;
}

.branch-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
