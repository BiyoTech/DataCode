<script setup lang="ts">
import { ref } from 'vue'
import { useGitStore } from '@/stores/git'
import { useWorkspaceStore } from '@/stores/workspace'

const gitStore = useGitStore()
const workspaceStore = useWorkspaceStore()

const showPublishForm = ref(false)
const repoName = ref('')
const isPrivate = ref(true)

function openPublish() {
  // Default repo name from workspace name
  const ws = workspaceStore.currentWorkspace
  repoName.value = ws?.name?.replace(/\s+/g, '-').toLowerCase() || 'my-project'
  showPublishForm.value = true
}

async function doPublish() {
  if (!repoName.value.trim()) return
  // Fall back to GitHub: it is the provider every credential form defaults to,
  // and unlike a self-hosted provider it needs no server URL to be usable.
  const provider = workspaceStore.gitCredential?.provider || 'github'
  await gitStore.publish(repoName.value.trim(), provider, isPrivate.value)
  showPublishForm.value = false
}
</script>

<template>
  <div class="init-panel">
    <div class="init-message">
      <p class="init-title">Initialize Repository</p>
      <p class="init-desc">
        This workspace doesn't have a Git repository yet. Initialize one to enable version control.
      </p>
    </div>

    <button class="init-btn primary" @click="gitStore.initRepo()" :disabled="gitStore.isLoading">
      {{ gitStore.isLoading ? 'Initializing...' : 'Initialize Repository' }}
    </button>

    <div class="divider">
      <span>or publish to remote</span>
    </div>

    <div v-if="!showPublishForm">
      <button class="init-btn" @click="openPublish">
        <span>🚀</span> Publish to {{ workspaceStore.gitCredential?.provider || 'remote' }}
      </button>
    </div>

    <div v-else class="publish-form">
      <div class="form-field">
        <label>Repository Name</label>
        <input v-model="repoName" type="text" placeholder="my-project" />
      </div>
      <label class="checkbox-label">
        <input type="checkbox" v-model="isPrivate" />
        <span>Private repository</span>
      </label>
      <div class="form-actions">
        <button class="btn-cancel" @click="showPublishForm = false">Cancel</button>
        <button class="btn-primary" @click="doPublish" :disabled="!repoName.trim() || gitStore.isLoading">
          {{ gitStore.isLoading ? 'Publishing...' : 'Publish' }}
        </button>
      </div>
    </div>

    <p v-if="gitStore.error" class="error-msg">{{ gitStore.error }}</p>
  </div>
</template>

<style scoped>
.init-panel {
  padding: 16px 12px;
  overflow-y: auto;
  flex: 1;
}

.init-message {
  margin-bottom: 16px;
}

.init-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.init-desc {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.4;
}

.init-btn {
  width: 100%;
  padding: 10px;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.init-btn:hover {
  border-color: var(--accent);
}

.init-btn.primary {
  background: var(--accent);
  color: var(--accent-foreground);
  font-weight: 600;
  border: none;
}

.init-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.divider {
  display: flex;
  align-items: center;
  margin: 16px 0;
  gap: 8px;
}

.divider::before,
.divider::after {
  content: '';
  flex: 1;
  border-top: 1px solid var(--border);
}

.divider span {
  font-size: 11px;
  color: var(--text-muted);
}

.publish-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 8px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-field label {
  font-size: 11px;
  color: var(--text-secondary);
}

.form-field input {
  padding: 7px 10px;
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}

.form-field input:focus {
  border-color: var(--accent);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.form-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.btn-cancel {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  padding: 6px 12px;
  font-size: 12px;
  cursor: pointer;
}

.btn-primary {
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: var(--accent-foreground);
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.error-msg {
  margin-top: 12px;
  font-size: 12px;
  color: var(--error);
}
</style>
