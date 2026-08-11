<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()

const showDropdown = ref(false)
const showCreateDialog = ref(false)
const newWorkspaceName = ref('')
const newWorkspaceDialect = ref('duckdb')
const newWorkspaceGitUrl = ref('')
const isCreating = ref(false)

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

function selectWorkspace(id: string) {
  if (id === workspaceStore.currentWorkspaceId) {
    showDropdown.value = false
    return
  }
  workspaceStore.selectWorkspace(id)
  showDropdown.value = false
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
        :class="['ws-item', { active: ws.id === workspaceStore.currentWorkspaceId }]"
        @click="selectWorkspace(ws.id)"
      >
        <span class="ws-item-icon">📂</span>
        <div class="ws-item-info">
          <span class="ws-item-name">{{ ws.name }}</span>
          <span class="ws-item-path">{{ ws.path }}</span>
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
        </div>
        <p class="dialog-hint">
          {{ t('workspace.pathHint') }}: ~/{{ 'user-admin-001' }}/{{ newWorkspaceName.replace(/\s+/g, '_').toLowerCase() || '...' }}/
        </p>
        <div class="dialog-actions">
          <button class="btn-cancel" @click="closeCreateDialog">{{ t('common.cancel') }}</button>
          <button class="btn-primary" @click="createWorkspace" :disabled="!newWorkspaceName.trim() || isCreating">
            {{ isCreating ? t('common.loading') : t('common.create') }}
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

.ws-item-path {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
</style>
