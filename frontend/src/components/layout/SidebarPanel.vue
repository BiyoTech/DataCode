<script setup lang="ts">
/**
 * SidebarPanel - File tree explorer with tabs for Files, Models, Changes.
 * Plan button navigates to the Plan & Apply page.
 * Integrates with FileStore for real file operations.
 */
import { watch, ref, computed, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { extractApiMessage } from '@/utils/notifyError'
import router from '@/router'
import { useFileStore, type FileNode } from '@/stores/files'
import { useWorkspaceStore } from '@/stores/workspace'
import { useGitStore } from '@/stores/git'
import { useFileMention } from '@/composables/useFileMention'
import {
  baseName,
  buildGitDecorations,
  dirName,
  kindOfStatus,
  letterOfStatus,
} from '@/utils/gitDecoration'
import FileTreeNode from './FileTreeNode.vue'
import ImportExternalModelsDialog from './ImportExternalModelsDialog.vue'
import IconButton from '@/components/common/IconButton.vue'

const { t } = useI18n()
const fileStore = useFileStore()
const workspaceStore = useWorkspaceStore()
const gitStore = useGitStore()
const { requestMention } = useFileMention()

// Highlight the Models activity-bar button when on the Models page.
const isModelsPage = computed(() => router.currentRoute.value.path === '/app/models')
// Highlight the Plan button when on the Plan page.
const isPlanPage = computed(() => router.currentRoute.value.path === '/app/plan')
// Highlight the A-BI button when on the A-BI module page.
const isAbiPage = computed(() => router.currentRoute.value.path === '/app/abi')
// Highlight the Asset Dag button when on the Asset Dag module page.
const isAssetDagPage = computed(() => router.currentRoute.value.path === '/app/asset-dag')

// The main editor view is shown on any route that is not a dedicated nav page.
const isMainEditorRoute = computed(
  () => !isModelsPage.value && !isPlanPage.value && !isAbiPage.value && !isAssetDagPage.value,
)

// Git decorations for the file tree: `path → decoration`, with every ancestor
// directory inheriting its most significant descendant so collapsed folders
// still signal inner changes. Built once per status refresh, so each tree node
// only does an O(1) lookup.
const gitDecorations = computed(() => buildGitDecorations(gitStore.changes))

// Changes list, split into the two groups the panel renders.
const stagedChanges = computed(() => gitStore.changes.filter((c) => c.staged))
const unstagedChanges = computed(() => gitStore.changes.filter((c) => !c.staged))

/** Row label: file name, with the parent directory shown as dimmed context. */
function changeName(path: string): string {
  return baseName(path)
}

function changeDir(path: string): string {
  return dirName(path)
}

/** One-letter badge + colour class, shared with the file tree decoration model. */
function changeLetter(status: string): string {
  return letterOfStatus(status)
}

function changeClass(status: string): string {
  const kind = kindOfStatus(status)
  return kind ? `git-${kind}` : ''
}

/** Open the changed file in the editor. Deleted paths have nothing to open. */
function handleChangeClick(path: string, status: string): void {
  if (kindOfStatus(status) === 'deleted') return
  handleOpenFile(path)
}

withDefaults(defineProps<{
  activeTab: 'files' | 'models' | 'diff'
  // When true, only the activity bar is rendered (used by the dedicated Models page).
  navOnly?: boolean
}>(), {
  navOnly: false,
})

const emit = defineEmits<{
  (e: 'change-tab', tab: 'files' | 'models' | 'diff'): void
  (e: 'toggle-bottom', tab: 'git' | 'log'): void
}>()

/**
 * Switch sidebar tab. When triggered from a dedicated page (Models/Plan), the
 * sidebar content is hidden and the editor area is occupied by that page, so we
 * first navigate back to the main editor route before emitting the tab change.
 */
function selectTab(tab: 'files' | 'diff') {
  if (!isMainEditorRoute.value) {
    router.push('/app/chat')
  }
  emit('change-tab', tab)
}

// Context menu state
const contextMenu = ref<{ visible: boolean; x: number; y: number; node: FileNode | null }>({
  visible: false, x: 0, y: 0, node: null,
})

// New file/dir dialog state
const showNewDialog = ref(false)
const newItemName = ref('')
const newItemType = ref<'file' | 'directory'>('file')
const newItemParent = ref('')

// Rename dialog state
const showRenameDialog = ref(false)
const renameValue = ref('')
const renameTarget = ref<FileNode | null>(null)

// Import external models dialog
const importDialogVisible = ref(false)

// Auto-refresh polling timer for file tree
let refreshTimer: ReturnType<typeof setInterval> | null = null

// Load file tree when workspace changes, and start auto-refresh polling
watch(
  () => workspaceStore.currentWorkspace,
  (ws) => {
    // Clear any existing timer
    if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }

    if (ws?.path) {
      fileStore.loadFileTree(ws.path)
      // Git decorations must follow the tree, so the status is refreshed on the
      // same cadence: otherwise a file edited outside the Git panel would keep
      // its stale colour.
      gitStore.fetchStatus()
      // Poll every 3 seconds to pick up external file changes
      refreshTimer = setInterval(() => {
        fileStore.refreshFileTree(ws.path)
        gitStore.fetchStatus()
      }, 3000)
    }
  },
  { immediate: true }
)

// Cleanup timer when component unmounts
onUnmounted(() => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
})

function handleToggleDir(node: FileNode) {
  const ws = workspaceStore.currentWorkspace
  if (!ws) return
  fileStore.toggleDirectory(ws.path, node)
}

function handleOpenFile(path: string) {
  const ws = workspaceStore.currentWorkspace
  if (!ws) return
  fileStore.openFile(ws.path, path)
}

function handleContextMenu(event: MouseEvent, node: FileNode) {
  contextMenu.value = { visible: true, x: event.clientX, y: event.clientY, node }
}

function closeContextMenu() {
  contextMenu.value.visible = false
}

function handleMentionInChat() {
  const node = contextMenu.value.node
  if (!node) return
  // Files and directories alike: the mention is a path token, and the agent
  // resolves a directory with its list tools rather than reading it.
  requestMention(node.path)
  closeContextMenu()
}

// Upload target: the directory the context menu was opened on. Remembered at
// click time because the picker fires its change event asynchronously.
const uploadInput = ref<HTMLInputElement | null>(null)
const uploadTargetDir = ref('')

function handleUploadClick() {
  const node = contextMenu.value.node
  if (!node || !node.isDirectory) return
  uploadTargetDir.value = node.path
  closeContextMenu()
  uploadInput.value?.click()
}

async function handleUploadPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const ws = workspaceStore.currentWorkspace
  const picked = Array.from(input.files || [])
  // Reset immediately so picking the same file twice still fires `change`.
  input.value = ''
  if (!ws || picked.length === 0) return

  const dir = uploadTargetDir.value
  const failures: string[] = []
  for (const file of picked) {
    try {
      await fileStore.uploadFile(ws.path, dir, file)
    } catch (e: any) {
      failures.push(`${file.name}: ${extractApiMessage(e, t('files.uploadFailed'))}`)
    }
  }

  await fileStore.loadFileTree(ws.path)
  const succeeded = picked.length - failures.length
  if (succeeded > 0) {
    MessagePlugin.success(t('files.uploaded', { count: succeeded, dir: dir || '/' }))
  }
  // One message per failure: the reason (oversize, invalid name) is per file.
  for (const failure of failures) {
    MessagePlugin.error(failure)
  }
}

function openNewDialog(type: 'file' | 'directory') {
  const parent = contextMenu.value.node
  newItemType.value = type
  newItemParent.value = parent?.isDirectory ? parent.path : ''
  newItemName.value = ''
  showNewDialog.value = true
  closeContextMenu()
}

async function confirmCreate() {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !newItemName.value.trim()) return

  const path = newItemParent.value
    ? `${newItemParent.value}/${newItemName.value.trim()}`
    : newItemName.value.trim()

  if (newItemType.value === 'directory') {
    await fileStore.createDirectory(ws.path, path)
  } else {
    await fileStore.createFile(ws.path, path)
  }

  // Refresh tree
  await fileStore.loadFileTree(ws.path)
  showNewDialog.value = false
}

function openRenameDialog() {
  const node = contextMenu.value.node
  if (!node) return
  renameTarget.value = node
  renameValue.value = node.name
  showRenameDialog.value = true
  closeContextMenu()
}

async function confirmRename() {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !renameTarget.value || !renameValue.value.trim()) return

  const oldPath = renameTarget.value.path
  const parts = oldPath.split('/')
  parts[parts.length - 1] = renameValue.value.trim()
  const newPath = parts.join('/')

  await fileStore.renameFile(ws.path, oldPath, newPath)
  await fileStore.loadFileTree(ws.path)
  showRenameDialog.value = false
}

async function handleDelete() {
  const ws = workspaceStore.currentWorkspace
  const node = contextMenu.value.node
  if (!ws || !node) return
  closeContextMenu()

  // The only irreversible action in the explorer, and the backend's delete is
  // recursive for directories — so it is named twice (title + body: the folder
  // itself and everything inside it) before a single request is sent.
  const dialog = DialogPlugin.confirm({
    header: node.isDirectory ? t('files.deleteDirTitle') : t('files.deleteFileTitle'),
    body: node.isDirectory
      ? t('files.deleteDirConfirm', { name: node.name })
      : t('files.deleteFileConfirm', { name: node.name }),
    theme: 'warning',
    confirmBtn: t('common.delete'),
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      dialog.hide()
      try {
        await fileStore.deleteFile(ws.path, node.path)
        await fileStore.loadFileTree(ws.path)
        MessagePlugin.success(t('files.deleted'))
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, 'Delete failed'))
      }
    },
  })
}

async function handleRefresh() {
  const ws = workspaceStore.currentWorkspace
  if (!ws) return
  await fileStore.loadFileTree(ws.path)
}

async function handleImportSuccess() {
  importDialogVisible.value = false
  const ws = workspaceStore.currentWorkspace
  if (!ws) return
  await fileStore.loadFileTree(ws.path)
}
</script>

<template>
  <div class="sidebar-panel" @click="closeContextMenu">
    <!-- Activity Bar (IntelliJ-style: upper icons + bottom icons) -->
    <div class="sidebar-tabs">
      <div class="tabs-top">
        <IconButton
          :class="{ active: isMainEditorRoute && activeTab === 'files' }"
          label="Files"
          @click="selectTab('files')"
        ><t-icon name="folder" size="18px" /></IconButton>
        <IconButton
          :class="{ active: isMainEditorRoute && activeTab === 'diff' }"
          label="Changes"
          @click="selectTab('diff')"
        >
          <t-icon name="swap" size="16px" />
          <span v-if="gitStore.changes.length > 0" class="badge-dot">{{ gitStore.changes.length }}</span>
        </IconButton>
        <IconButton
          :class="{ active: isModelsPage }"
          label="Models"
          class="plan-btn"
          @click="router.push('/app/models')"
        ><t-icon name="chart-bar" size="18px" /></IconButton>
        <IconButton
          :class="{ active: isAbiPage }"
          label="A-BI"
          class="abi-btn"
          @click="router.push('/app/abi')"
        ><t-icon name="chart-analytics" size="18px" /></IconButton>
        <IconButton
          :class="{ active: isAssetDagPage }"
          label="Asset Dag"
          class="plan-btn"
          @click="router.push('/app/asset-dag')"
        ><t-icon name="chart-bubble" size="18px" /></IconButton>
        <IconButton
          :class="{ active: isPlanPage }"
          label="Plan & Apply"
          class="plan-btn"
          @click="router.push('/app/plan')"
        ><t-icon name="play-circle" size="16px" /></IconButton>
      </div>
      <div class="tabs-bottom">
        <IconButton label="Git" @click="$emit('toggle-bottom', 'git')">
          <t-icon name="git-branch" size="18px" />
        </IconButton>
        <IconButton label="Log" @click="$emit('toggle-bottom', 'log')">
          <t-icon name="history" size="18px" />
        </IconButton>
      </div>
    </div>

    <!-- Panel content (hidden when only the activity bar is needed, e.g. Models page) -->
    <div v-if="!navOnly" class="sidebar-content">
      <!-- Files tab -->
      <template v-if="activeTab === 'files'">
        <div class="section-header">
          <span>EXPLORER</span>
          <div class="header-actions">
            <IconButton class="icon-btn" label="New File" @click="openNewDialog('file')"><t-icon name="file-add" size="14px" /></IconButton>
            <IconButton class="icon-btn" label="New Folder" @click="openNewDialog('directory')"><t-icon name="folder-add" size="14px" /></IconButton>
            <IconButton class="icon-btn" label="Refresh" @click="handleRefresh"><t-icon name="refresh" size="14px" /></IconButton>
            <IconButton class="icon-btn" :label="t('externalModels.import.button')" @click="importDialogVisible = true"><t-icon name="cloud-download" size="14px" /></IconButton>
          </div>
        </div>

        <!-- Loading state -->
        <div v-if="fileStore.isLoadingTree" class="loading-state">
          Loading...
        </div>

        <!-- Empty state -->
        <div v-else-if="fileStore.fileTree.length === 0" class="empty-state">
          {{ workspaceStore.currentWorkspace ? t('workspace.noFiles') : t('workspace.selectOrCreate') }}
        </div>

        <!-- File tree -->
        <div v-else class="file-tree">
          <FileTreeNode
            v-for="node in fileStore.fileTree"
            :key="node.path"
            :node="node"
            :depth="0"
            :git-decorations="gitDecorations"
            @toggle-dir="handleToggleDir"
            @open-file="handleOpenFile"
            @context-menu="handleContextMenu"
          />
        </div>
      </template>

      <!-- Diff / Changes tab -->
      <template v-if="activeTab === 'diff'">
        <div class="section-header">
          <span>CHANGES</span>
          <div class="header-actions">
            <IconButton class="icon-btn" label="Stage All" @click="gitStore.stageFiles([])"><t-icon name="add" size="16px" /></IconButton>
            <IconButton class="icon-btn" label="Refresh" @click="gitStore.fetchStatus()"><t-icon name="refresh" size="14px" /></IconButton>
          </div>
        </div>

        <!-- Commit input -->
        <div class="diff-commit">
          <input
            v-model="gitStore.commitMessage"
            class="diff-commit-input"
            :placeholder="t('git.commitPlaceholder')"
            @keydown.enter.ctrl="gitStore.commit()"
          />
          <IconButton
            class="diff-commit-btn"
            :label="t('git.commit')"
            :disabled="!gitStore.commitMessage.trim() || stagedChanges.length === 0"
            @click="gitStore.commit()"
          ><t-icon name="check" size="14px" /></IconButton>
        </div>

        <!-- Staged files -->
        <div v-if="stagedChanges.length > 0" class="diff-group">
          <div class="diff-group-title">
            <span>{{ t('git.stagedChanges') }}</span>
            <span class="diff-count">{{ stagedChanges.length }}</span>
          </div>
          <div
            v-for="change in stagedChanges"
            :key="'s-' + change.path"
            class="diff-item"
            :title="change.path"
            @click="handleChangeClick(change.path, change.status)"
          >
            <span class="diff-status" :class="changeClass(change.status)">
              {{ changeLetter(change.status) }}
            </span>
            <span class="diff-filename" :class="changeClass(change.status)">
              {{ changeName(change.path) }}
            </span>
            <span v-if="changeDir(change.path)" class="diff-filedir">
              {{ changeDir(change.path) }}
            </span>
            <IconButton class="diff-action" :label="t('git.unstage')" @click.stop="gitStore.unstageFiles([change.path])"><t-icon name="minus" size="14px" /></IconButton>
          </div>
        </div>

        <!-- Unstaged files -->
        <div v-if="unstagedChanges.length > 0" class="diff-group">
          <div class="diff-group-title">
            <span>{{ t('git.changes') }}</span>
            <span class="diff-count">{{ unstagedChanges.length }}</span>
          </div>
          <div
            v-for="change in unstagedChanges"
            :key="'u-' + change.path"
            class="diff-item"
            :title="change.path"
            @click="handleChangeClick(change.path, change.status)"
          >
            <span class="diff-status" :class="changeClass(change.status)">
              {{ changeLetter(change.status) }}
            </span>
            <span class="diff-filename" :class="changeClass(change.status)">
              {{ changeName(change.path) }}
            </span>
            <span v-if="changeDir(change.path)" class="diff-filedir">
              {{ changeDir(change.path) }}
            </span>
            <IconButton class="diff-action" :label="t('git.stage')" @click.stop="gitStore.stageFiles([change.path])"><t-icon name="add" size="14px" /></IconButton>
          </div>
        </div>

        <!-- Clean state -->
        <div v-if="gitStore.isClean && !gitStore.isLoading" class="empty-state">
          <t-icon name="check-circle" size="16px" style="margin-right: 4px; vertical-align: middle;" /> {{ t('git.noChanges') }}
        </div>
      </template>

    </div>

    <!-- Context menu -->
    <div
      v-if="contextMenu.visible"
      class="context-menu"
      :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
      @click.stop
    >
      <div class="ctx-item" @click="openNewDialog('file')">New File</div>
      <div class="ctx-item" @click="openNewDialog('directory')">New Folder</div>
      <div class="ctx-divider" />
      <!-- Directory-only: the target of an upload is the folder the menu was
           opened on. -->
      <div
        v-if="contextMenu.node && contextMenu.node.isDirectory"
        class="ctx-item"
        @click="handleUploadClick"
      >
        {{ t('files.uploadHere') }}
      </div>
      <!-- Files and directories alike: a mentioned folder is something the
           agent can list with its file tools, not just read. -->
      <div class="ctx-item" @click="handleMentionInChat">
        {{ t('chat.mention.reference') }}
      </div>
      <div class="ctx-divider" />
      <div class="ctx-item" @click="openRenameDialog">Rename</div>
      <div class="ctx-item ctx-danger" @click="handleDelete">Delete</div>
    </div>

    <!-- Hidden picker behind "Upload files here"; the target directory is
         remembered when the menu item is clicked. -->
    <input
      ref="uploadInput"
      type="file"
      multiple
      class="hidden-file-input"
      @change="handleUploadPicked"
    />

    <!-- New file/directory dialog -->
    <div v-if="showNewDialog" class="dialog-overlay" @click.self="showNewDialog = false">
      <div class="dialog">
        <h3>{{ newItemType === 'file' ? 'New File' : 'New Folder' }}</h3>
        <div class="dialog-field">
          <label>Name</label>
          <input
            v-model="newItemName"
            type="text"
            :placeholder="newItemType === 'file' ? 'filename.sql' : 'folder_name'"
            @keydown.enter="confirmCreate"
            autofocus
          />
        </div>
        <p v-if="newItemParent" class="dialog-hint">
          In: {{ newItemParent }}/
        </p>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="showNewDialog = false">Cancel</button>
          <button class="btn-primary" @click="confirmCreate" :disabled="!newItemName.trim()">Create</button>
        </div>
      </div>
    </div>

    <!-- Rename dialog -->
    <div v-if="showRenameDialog" class="dialog-overlay" @click.self="showRenameDialog = false">
      <div class="dialog">
        <h3>Rename</h3>
        <div class="dialog-field">
          <label>New name</label>
          <input
            v-model="renameValue"
            type="text"
            @keydown.enter="confirmRename"
            autofocus
          />
        </div>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="showRenameDialog = false">Cancel</button>
          <button class="btn-primary" @click="confirmRename" :disabled="!renameValue.trim()">Rename</button>
        </div>
      </div>
    </div>

    <!-- Import external models dialog -->
    <ImportExternalModelsDialog
      :visible="importDialogVisible"
      @update:visible="importDialogVisible = $event"
      @success="handleImportSuccess"
    />
  </div>
</template>

<style scoped>
.sidebar-panel {
  display: flex;
  height: 100%;
}

.sidebar-tabs {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  width: 40px;
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  padding: 8px 0;
  align-items: center;
}

.tabs-top,
.tabs-bottom {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.sidebar-tabs button {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 8px;
  border-radius: 6px;
  font-size: 16px;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.sidebar-tabs button:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.sidebar-tabs button.active {
  color: var(--accent);
  background: var(--bg-hover);
}

.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  letter-spacing: 0.5px;
}

.header-actions {
  display: flex;
  gap: 2px;
}

.icon-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 11px;
  padding: 2px 4px;
  border-radius: 3px;
}

.icon-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.file-tree {
  padding: 0 4px;
}

.loading-state,
.empty-state {
  padding: 20px 12px;
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
}

/* Context menu */
.context-menu {
  position: fixed;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: var(--shadow-float);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.ctx-item {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: pointer;
}

.ctx-item:hover {
  background: var(--bg-hover);
}

.ctx-danger {
  color: var(--error);
}

.ctx-divider {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}

/* Visually hidden but still focusable/clickable programmatically. */
.hidden-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

/* Dialog */
.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.dialog {
  width: 360px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
}

.dialog h3 {
  font-size: 16px;
  margin-bottom: 16px;
  color: var(--text-primary);
}

.dialog-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.dialog-field label {
  font-size: 12px;
  color: var(--text-secondary);
}

.dialog-field input {
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
}

.dialog-field input:focus {
  border-color: var(--accent);
}

.dialog-hint {
  font-size: 11px;
  color: var(--text-muted);
  font-family: monospace;
  margin-bottom: 16px;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-cancel {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
}

.btn-primary {
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: var(--accent-foreground);
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Activity bar button with badge */
.sidebar-tabs button {
  position: relative;
}

.diff-icon {
  font-size: 14px;
}

.badge-dot {
  position: absolute;
  top: 2px;
  right: 2px;
  background: var(--accent);
  color: var(--bg-surface);
  font-size: 8px;
  font-weight: 700;
  min-width: 14px;
  height: 14px;
  border-radius: 7px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 3px;
}

/* Diff tab styles */
.diff-commit {
  display: flex;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}

.diff-commit-input {
  flex: 1;
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
  color: var(--text-primary);
  font-size: 11px;
  outline: none;
}

.diff-commit-input:focus {
  border-color: var(--accent);
}

.diff-commit-btn {
  background: var(--accent);
  color: var(--bg-surface);
  border: none;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.diff-commit-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.diff-group {
  margin-bottom: 4px;
}

.diff-group-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  background: var(--bg-surface);
}

.diff-count {
  background: var(--accent-dim);
  color: var(--text-secondary);
  padding: 0 5px;
  border-radius: 8px;
  font-size: 9px;
}

.diff-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px 3px 14px;
  font-size: 12px;
  cursor: pointer;
}

.diff-item:hover {
  background: var(--bg-hover);
}

.diff-item:hover .diff-action {
  opacity: 1;
}

.diff-status {
  font-size: 10px;
  font-weight: 700;
  font-family: var(--font-mono);
  width: 12px;
  text-align: center;
  flex-shrink: 0;
}

/* Git decoration colours, shared with the file tree. */
.git-untracked { color: var(--git-untracked); }
.git-added { color: var(--git-added); }
.git-modified { color: var(--git-modified); }
.git-renamed { color: var(--git-renamed); }
.git-deleted { color: var(--git-deleted); }
.git-conflict { color: var(--git-conflict); font-weight: 600; }

.diff-filename.git-deleted {
  text-decoration: line-through;
}

.diff-filename {
  flex-shrink: 0;
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}

/* Parent directory shown as dimmed context, truncated from the left so the
 * closest folder stays visible on narrow sidebars. */
.diff-filedir {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  direction: rtl;
  text-align: left;
  font-size: 11px;
  color: var(--text-muted);
}

.diff-action {
  opacity: 0;
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 1px 4px;
  border-radius: 3px;
}

.diff-action:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

/* Plan button */
.plan-btn {
  font-size: 12px;
  color: var(--success);
  transition: transform 0.15s ease, color 0.15s ease;
}

.plan-btn:hover {
  color: var(--accent);
  transform: scale(1.15);
}

/* A-BI module button */
.abi-btn {
  color: var(--accent);
  transition: transform 0.15s ease, color 0.15s ease;
}

.abi-btn:hover {
  color: var(--accent);
  transform: scale(1.15);
}

</style>
