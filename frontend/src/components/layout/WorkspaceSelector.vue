<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore, type WorkspaceInfo } from '@/stores/workspace'
import { usePermission } from '@/composables/usePermission'
import { notifyError } from '@/utils/notifyError'
import WorkspaceMembersDialog from './WorkspaceMembersDialog.vue'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const { canInWorkspace } = usePermission()

const showDropdown = ref(false)
const showCreateDialog = ref(false)
const newWorkspaceName = ref('')
const newWorkspaceDialect = ref('duckdb')
const newWorkspaceGitUrl = ref('')
const isCreating = ref(false)

// Environment picker shown when a workspace offers more than one development
// line. Users pick an environment, never a raw branch.
const environmentPicker = ref<{
  workspaceId: string
  environments: { environmentId: string; name: string; envType: string; status: string }[]
  selected: string
} | null>(null)
// Member management target (owner only).
const membersTarget = ref<WorkspaceInfo | null>(null)
// Git binding target and input.
const gitBindTarget = ref<WorkspaceInfo | null>(null)
const gitBindUrl = ref('')
const isGitBinding = ref(false)
// Deletion confirmation target.
const deleteTarget = ref<WorkspaceInfo | null>(null)
const isDeleting = ref(false)

// Supported SQL engine types
const dialectOptions = [
  { value: 'duckdb', label: 'DuckDB' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'spark', label: 'Spark' },
  { value: 'trino', label: 'Trino' },
  { value: 'mssql', label: 'MSSQL' },
  { value: 'postgres', label: 'Postgres' },
  { value: 'mysql', label: 'MySQL' },
]

// Load workspaces from backend on mount
onMounted(() => {
  workspaceStore.fetchWorkspaces()
})

function toggleDropdown() {
  showDropdown.value = !showDropdown.value
}

/** Human-readable reason why a workspace cannot be opened. */
function blockedTooltip(ws: WorkspaceInfo): string {
  return ws.openBlockedReason === 'no_git_repo'
    ? t('workspace.contactOwnerToPush')
    : t('workspace.notOpenable')
}

/**
 * Subtitle: the Git remote plus the environment currently open. Before the
 * workspace is opened only the remote is known, so the default environment is
 * shown as what opening it would land on.
 */
function repoLabel(ws: WorkspaceInfo): string {
  if (!ws.gitRepoUrl) return t('workspace.noGitRepo')
  return `${ws.gitRepoUrl} · ${ws.currentBranch || ws.defaultEnv}`
}

/**
 * Open a workspace.
 *
 * When it offers several environments, ask which development line to open
 * instead of silently defaulting: the environment decides both the code on disk
 * and the data views, so guessing it would be guessing what the user works on.
 * The last environment used in that workspace is preselected, so returning to a
 * workspace returns to the line the user left. A single-environment workspace
 * opens straight into it (the workspace default).
 */
async function selectWorkspace(ws: WorkspaceInfo) {
  if (!ws.openable) return
  showDropdown.value = false

  if (ws.id === workspaceStore.currentWorkspaceId && ws.path) return

  try {
    const environments = await workspaceStore.fetchOpenableEnvironments(ws.id)
    if (environments.length > 1) {
      const remembered = workspaceStore.rememberedEnvironment(ws.id)
      const stillOpen = environments.some((env) => env.name === remembered)
      const production = environments.find((env) => env.envType === 'production')
      environmentPicker.value = {
        workspaceId: ws.id,
        environments,
        selected: (stillOpen && remembered) || (production || environments[0]).name,
      }
      return
    }
    await workspaceStore.selectWorkspace(ws.id, environments[0]?.name || '')
  } catch (e) {
    notifyError(e, t('workspace.openFailed'))
  }
}

async function confirmEnvironment() {
  const picker = environmentPicker.value
  if (!picker) return
  try {
    await workspaceStore.selectWorkspace(picker.workspaceId, picker.selected)
    environmentPicker.value = null
  } catch (e) {
    notifyError(e, t('workspace.openFailed'))
  }
}

function openMembersDialog(ws: WorkspaceInfo) {
  showDropdown.value = false
  membersTarget.value = ws
}

/** Whether the caller may bind a Git remote to this workspace. */
function canBindGit(ws: WorkspaceInfo): boolean {
  return canInWorkspace('workspace:git:bind', ws.id)
}

/** Whether the caller may delete this workspace. */
function canDeleteWorkspace(ws: WorkspaceInfo): boolean {
  return canInWorkspace('workspace:delete', ws.id)
}

function openGitBindDialog(ws: WorkspaceInfo) {
  if (!canBindGit(ws)) return
  showDropdown.value = false
  gitBindTarget.value = ws
  gitBindUrl.value = ws.gitRepoUrl || ''
}

async function confirmGitBind() {
  const ws = gitBindTarget.value
  const url = gitBindUrl.value.trim()
  if (!ws || !url || isGitBinding.value) return
  isGitBinding.value = true
  try {
    await workspaceStore.bindGitRepo(ws.id, url)
    gitBindTarget.value = null
    gitBindUrl.value = ''
  } catch (e) {
    notifyError(e, t('workspace.bindGitFailed'))
  } finally {
    isGitBinding.value = false
  }
}

function openDeleteDialog(ws: WorkspaceInfo) {
  if (!canDeleteWorkspace(ws)) return
  showDropdown.value = false
  deleteTarget.value = ws
}

async function confirmDelete() {
  const ws = deleteTarget.value
  if (!ws || isDeleting.value) return
  isDeleting.value = true
  try {
    await workspaceStore.deleteWorkspace(ws.id)
    deleteTarget.value = null
  } catch (e) {
    notifyError(e, t('workspace.deleteFailed'))
  } finally {
    isDeleting.value = false
  }
}

function openCreateDialog() {
  showDropdown.value = false
  showCreateDialog.value = true
  newWorkspaceName.value = ''
  newWorkspaceDialect.value = 'duckdb'
  newWorkspaceGitUrl.value = ''
}

async function createWorkspace() {
  const name = newWorkspaceName.value.trim()
  if (!name || isCreating.value) return

  isCreating.value = true
  try {
    await workspaceStore.createWorkspace(name, newWorkspaceDialect.value, newWorkspaceGitUrl.value.trim())
    showCreateDialog.value = false
    newWorkspaceName.value = ''
    newWorkspaceGitUrl.value = ''
  } catch (e) {
    notifyError(e, t('workspace.createFailed'))
  } finally {
    isCreating.value = false
  }
}

function closeCreateDialog() {
  showCreateDialog.value = false
}
</script>

<template>
  <div class="workspace-selector">
    <!-- Current workspace button -->
    <button class="ws-btn" @click="toggleDropdown">
      <span class="ws-icon">📁</span>
      <span class="ws-name">
        {{ workspaceStore.currentWorkspace?.name || t('workspace.selectOrCreate') }}
      </span>
      <span class="ws-arrow">▾</span>
    </button>

    <!-- Dropdown -->
    <div v-if="showDropdown" class="ws-dropdown">
      <div class="ws-dropdown-header">{{ t('workspace.title') }}</div>

      <!-- Workspace list -->
      <div
        v-for="ws in workspaceStore.workspaces"
        :key="ws.id"
        :class="[
          'ws-item',
          {
            active: ws.id === workspaceStore.currentWorkspaceId,
            disabled: !ws.openable,
          },
        ]"
        :title="ws.openable ? ws.gitRepoUrl : blockedTooltip(ws)"
        @click="selectWorkspace(ws)"
      >
        <span class="ws-item-icon">{{ ws.openable ? '📂' : '🔒' }}</span>
        <div class="ws-item-info">
          <span class="ws-item-name">{{ ws.name }}</span>
          <span class="ws-item-repo">{{ repoLabel(ws) }}</span>
          <span v-if="!ws.openable" class="ws-item-blocked">{{ blockedTooltip(ws) }}</span>
        </div>
        <div class="ws-item-actions" @click.stop>
          <button
            class="ws-item-action"
            :class="{ 'is-disabled': !canBindGit(ws) }"
            :disabled="!canBindGit(ws)"
            :title="canBindGit(ws) ? t('workspace.bindGit') : t('workspace.noPermission')"
            @click="openGitBindDialog(ws)"
          >
            ⚙
          </button>
          <button
            v-if="ws.isOwner"
            class="ws-item-action"
            :title="t('workspace.members')"
            @click="openMembersDialog(ws)"
          >
            👥
          </button>
          <button
            class="ws-item-action ws-item-action-danger"
            :class="{ 'is-disabled': !canDeleteWorkspace(ws) }"
            :disabled="!canDeleteWorkspace(ws)"
            :title="canDeleteWorkspace(ws) ? t('workspace.delete') : t('workspace.noPermission')"
            @click="openDeleteDialog(ws)"
          >
            🗑
          </button>
        </div>
      </div>

      <div v-if="workspaceStore.workspaces.length === 0" class="ws-empty">
        {{ t('workspace.noWorkspaces') }}
      </div>

      <!-- Create button -->
      <div class="ws-dropdown-footer">
        <button class="ws-create-btn" @click="openCreateDialog">
          + {{ t('workspace.create') }}
        </button>
      </div>
    </div>

    <!-- Backdrop for dropdown -->
    <div v-if="showDropdown" class="ws-backdrop" @click="showDropdown = false" />

    <!-- Create workspace dialog -->
    <div v-if="showCreateDialog" class="dialog-overlay" @click.self="closeCreateDialog">
      <div class="dialog">
        <h3>{{ t('workspace.create') }}</h3>
        <div class="dialog-field">
          <label>{{ t('workspace.nameLabel') }}</label>
          <input
            v-model="newWorkspaceName"
            type="text"
            :placeholder="t('workspace.namePlaceholder')"
            @keydown.enter="createWorkspace"
            autofocus
          />
        </div>
        <div class="dialog-field">
          <label>{{ t('workspace.dialectLabel') }}</label>
          <select v-model="newWorkspaceDialect" class="dialect-select">
            <option
              v-for="opt in dialectOptions"
              :key="opt.value"
              :value="opt.value"
            >
              {{ opt.label }}
            </option>
          </select>
        </div>
        <div class="dialog-field">
          <label>{{ t('workspace.gitRepoUrl') }}</label>
          <input
            v-model="newWorkspaceGitUrl"
            type="text"
            :placeholder="t('workspace.gitRepoUrlPlaceholder')"
          />
          <span class="field-hint">{{ t('workspace.gitRepoUrlHint') }}</span>
          <span class="field-hint">{{ t('workspace.gitRepoAutoCreateHint') }}</span>
        </div>
        <p class="dialog-hint">{{ t('workspace.sharedHint') }}</p>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="closeCreateDialog">{{ t('common.cancel') }}</button>
          <button class="btn-primary" @click="createWorkspace" :disabled="!newWorkspaceName.trim() || isCreating">
            {{ isCreating ? t('common.loading') : t('common.create') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Environment picker dialog -->
    <div v-if="environmentPicker" class="dialog-overlay" @click.self="environmentPicker = null">
      <div class="dialog">
        <h3>{{ t('workspace.selectEnvironment') }}</h3>
        <div class="dialog-field">
          <label>{{ t('workspace.environmentLabel') }}</label>
          <select v-model="environmentPicker.selected">
            <option v-for="env in environmentPicker.environments" :key="env.environmentId" :value="env.name">
              {{ env.name }} · {{ t(`environment.type.${env.envType}`) }}
            </option>
          </select>
        </div>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="environmentPicker = null">{{ t('common.cancel') }}</button>
          <button class="btn-primary" :disabled="workspaceStore.isOpening" @click="confirmEnvironment">
            {{ workspaceStore.isOpening ? t('common.loading') : t('workspace.open') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Member management dialog (owner only) -->
    <WorkspaceMembersDialog
      v-if="membersTarget"
      :workspace-id="membersTarget.id"
      :workspace-name="membersTarget.name"
      @close="membersTarget = null"
    />

    <!-- Git binding dialog -->
    <div v-if="gitBindTarget" class="dialog-overlay" @click.self="gitBindTarget = null">
      <div class="dialog">
        <h3>{{ t('workspace.bindGit') }} — {{ gitBindTarget.name }}</h3>
        <div class="dialog-field">
          <label>{{ t('workspace.gitRepoUrl') }}</label>
          <input
            v-model="gitBindUrl"
            type="text"
            :placeholder="t('workspace.gitRepoUrlPlaceholder')"
            autofocus
            @keydown.enter="confirmGitBind"
          />
          <span class="field-hint">{{ t('workspace.bindGitHint') }}</span>
        </div>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="gitBindTarget = null">{{ t('common.cancel') }}</button>
          <button
            class="btn-primary"
            :disabled="!gitBindUrl.trim() || isGitBinding"
            @click="confirmGitBind"
          >
            {{ isGitBinding ? t('common.loading') : t('workspace.bind') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Delete confirmation dialog -->
    <div v-if="deleteTarget" class="dialog-overlay" @click.self="deleteTarget = null">
      <div class="dialog">
        <h3>{{ t('workspace.deleteTitle') }}</h3>
        <p class="dialog-hint">
          {{ t('workspace.deleteConfirm', { name: deleteTarget.name }) }}
        </p>
        <p v-if="deleteTarget.gitRepoUrl" class="dialog-hint">
          {{ t('workspace.deleteKeepsRemote', { url: deleteTarget.gitRepoUrl }) }}
        </p>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="deleteTarget = null">{{ t('common.cancel') }}</button>
          <button class="btn-danger" :disabled="isDeleting" @click="confirmDelete">
            {{ isDeleting ? t('common.loading') : t('common.delete') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.workspace-selector {
  position: relative;
}

.ws-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 10px;
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
}

.ws-btn:hover {
  border-color: var(--accent-dim);
}

.ws-icon {
  font-size: 12px;
}

.ws-name {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ws-arrow {
  font-size: 10px;
  color: var(--text-muted);
}

.ws-backdrop {
  position: fixed;
  inset: 0;
  z-index: 99;
}

.ws-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  width: 320px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float-lg);
  z-index: 100;
  overflow: hidden;
}

.ws-dropdown-header {
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.ws-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
}

.ws-item:hover {
  background: var(--bg-hover);
}

.ws-item.active {
  background: var(--bg-hover);
  border-left: 2px solid var(--accent);
}

.ws-item.disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.ws-item.disabled:hover {
  background: none;
}

.ws-item-icon {
  font-size: 14px;
  margin-top: 2px;
}

.ws-item-info {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex: 1;
}

.ws-item-name {
  font-size: 13px;
  color: var(--text-primary);
}

.ws-item-repo {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ws-item-blocked {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 2px;
  white-space: normal;
}

.ws-item-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 1px;
}

.ws-item-action {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  padding: 0 2px;
}

.ws-item-action:hover {
  color: var(--text-primary);
}

.ws-item-action.is-disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.ws-item-action.is-disabled:hover {
  color: var(--text-muted);
}

.ws-item-action-danger:hover:not(.is-disabled) {
  color: var(--danger, #e34d59);
}

.btn-danger {
  background: var(--danger, #e34d59);
  border: none;
  border-radius: 6px;
  color: #fff;
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.btn-danger:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.ws-empty {
  padding: 16px 12px;
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
}

.ws-dropdown-footer {
  border-top: 1px solid var(--border);
  padding: 8px 12px;
}

.ws-create-btn {
  width: 100%;
  background: none;
  border: 1px dashed var(--border);
  border-radius: 6px;
  color: var(--accent);
  font-size: 12px;
  padding: 6px;
  cursor: pointer;
}

.ws-create-btn:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
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
  width: 440px;
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

.dialog-field input,
.dialog-field select {
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
}

.dialog-field input:focus,
.dialog-field select:focus {
  border-color: var(--accent);
}

.field-hint {
  font-size: 11px;
  color: var(--text-muted);
}

.dialect-select {
  cursor: pointer;
  appearance: auto;
}

.dialect-select option {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.dialog-hint {
  font-size: 11px;
  color: var(--text-muted);
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
</style>
