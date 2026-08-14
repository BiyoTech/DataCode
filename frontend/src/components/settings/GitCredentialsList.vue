<script setup lang="ts">
/**
 * GitCredentialsList - full Git Credentials settings section.
 * Displays credential list + add modal; handles CRUD operations.
 */
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import type { CreateGitCredentialRequest } from '@/types/git-credentials'
import { notifyError } from '@/utils/notifyError'
import GitCredentialCard from './GitCredentialCard.vue'
import AddGitCredentialModal from './AddGitCredentialModal.vue'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()

const showAddModal = ref(false)
const deleteConfirmId = ref<string | null>(null)

onMounted(() => {
  workspaceStore.fetchGitCredentials()
})

async function handleAddCredential(req: CreateGitCredentialRequest) {
  try {
    await workspaceStore.createGitCredential(req)
    showAddModal.value = false
  } catch (e) {
    // Keep the modal open so the user can correct the input (duplicate host,
    // rejected auth type, ...) instead of losing what they typed.
    notifyError(e, t('settings.gitAddCredential'))
  }
}

function handleEdit(credentialId: string) {
  // For now, we show a simple prompt; can be extended to a full edit modal
  const cred = workspaceStore.gitCredentials.find(c => c.id === credentialId)
  if (!cred) return

  const newNickname = prompt(t('settings.gitEditNickname'), cred.nickname)
  if (newNickname !== null && newNickname.trim()) {
    workspaceStore.updateGitCredential(credentialId, { nickname: newNickname.trim() })
  }
}

function handleDelete(credentialId: string) {
  deleteConfirmId.value = credentialId
}

function confirmDelete() {
  if (deleteConfirmId.value) {
    workspaceStore.deleteGitCredential(deleteConfirmId.value)
    deleteConfirmId.value = null
  }
}

function cancelDelete() {
  deleteConfirmId.value = null
}

async function handleSetDefault(credentialId: string) {
  await workspaceStore.setDefaultGitCredential(credentialId)
}
</script>

<template>
  <div class="git-credentials-section">
    <div class="section-header">
      <div class="section-title-area">
        <h4 class="section-title">{{ t('settings.gitIntegration') }}</h4>
        <p class="section-desc">{{ t('settings.gitIntegrationDesc') }}</p>
      </div>
      <button class="btn-add" @click="showAddModal = true">
        + {{ t('settings.gitAddCredential') }}
      </button>
    </div>

    <!-- Loading state -->
    <div v-if="workspaceStore.isLoadingCredentials" class="loading-state">
      {{ t('common.loading') }}
    </div>

    <!-- Empty state -->
    <div v-else-if="workspaceStore.gitCredentials.length === 0" class="empty-state">
      <p class="empty-text">{{ t('settings.gitNoCredentials') }}</p>
      <button class="btn-add-empty" @click="showAddModal = true">
        {{ t('settings.gitAddCredential') }}
      </button>
    </div>

    <!-- Credential list -->
    <div v-else class="credentials-list">
      <GitCredentialCard
        v-for="cred in workspaceStore.gitCredentials"
        :key="cred.id"
        :credential="cred"
        @edit="handleEdit"
        @delete="handleDelete"
        @set-default="handleSetDefault"
      />
    </div>

    <!-- Delete confirmation dialog -->
    <Teleport to="body">
      <div v-if="deleteConfirmId" class="confirm-overlay" @click.self="cancelDelete">
        <div class="confirm-dialog">
          <p class="confirm-text">{{ t('settings.gitConfirmDelete') }}</p>
          <div class="confirm-actions">
            <button class="btn-cancel" @click="cancelDelete">{{ t('common.cancel') }}</button>
            <button class="btn-danger" @click="confirmDelete">{{ t('common.delete') }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Add modal -->
    <AddGitCredentialModal
      :visible="showAddModal"
      @close="showAddModal = false"
      @submit="handleAddCredential"
    />
  </div>
</template>

<style scoped>
.git-credentials-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.section-title-area {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.section-desc {
  font-size: 12px;
  color: var(--text-muted);
}

.btn-add {
  padding: 7px 14px;
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}

.btn-add:hover {
  filter: brightness(1.1);
}

.loading-state {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}

.empty-state {
  padding: 32px;
  text-align: center;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
}

.empty-text {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 12px;
}

.btn-add-empty {
  padding: 8px 16px;
  background: none;
  border: 1px solid var(--accent);
  border-radius: 6px;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
}

.btn-add-empty:hover {
  background: var(--accent-subtler);
}

.credentials-list {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}

/* Delete confirmation */
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}

.confirm-dialog {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 24px;
  min-width: 300px;
  box-shadow: var(--shadow-float-lg);
}

.confirm-text {
  font-size: 14px;
  color: var(--text-primary);
  margin-bottom: 20px;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
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

.btn-danger {
  padding: 7px 14px;
  background: var(--error);
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}

.btn-danger:hover {
  background: var(--error);
}
</style>
