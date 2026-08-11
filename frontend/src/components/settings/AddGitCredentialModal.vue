<script setup lang="ts">
/**
 * AddGitCredentialModal - modal for adding a new Git credential.
 * Supports two auth modes: Link Git Account (OAuth) and Personal Access Token (PAT).
 */
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { GitProvider, AuthType, CreateGitCredentialRequest } from '@/types/git-credentials'

const { t } = useI18n()

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'submit', req: CreateGitCredentialRequest): void
}>()

// Form state
const provider = ref<string>('github')
const customProvider = ref('')
const authType = ref<AuthType>('pat')
const nickname = ref('')
const token = ref('')
const email = ref('')
const username = ref('')
const serverUrl = ref('')
const isSaving = ref(false)

const providers: { value: string; label: string }[] = [
  { value: 'github', label: 'GitHub' },
  { value: 'gitlab', label: 'GitLab' },
  { value: 'bitbucket', label: 'Bitbucket' },
  { value: 'gitea', label: 'Gitea' },
  { value: 'custom', label: 'Custom' },
]

const effectiveProvider = computed(() => {
  return provider.value === 'custom' ? customProvider.value.trim().toLowerCase() : provider.value
})

const isValid = computed(() => {
  if (provider.value === 'custom' && !customProvider.value.trim()) return false
  if (authType.value === 'pat') {
    return token.value.trim().length > 0 && serverUrl.value.trim().length > 0
  }
  // OAuth would redirect, so always valid to initiate
  return true
})

function resetForm() {
  provider.value = 'github'
  customProvider.value = ''
  authType.value = 'pat'
  nickname.value = ''
  token.value = ''
  email.value = ''
  username.value = ''
  serverUrl.value = ''
  isSaving.value = false
}

function handleClose() {
  resetForm()
  emit('close')
}

async function handleSubmit() {
  if (!isValid.value || isSaving.value) return
  isSaving.value = true

  try {
    const req: CreateGitCredentialRequest = {
      provider: effectiveProvider.value as GitProvider,
      auth_type: authType.value,
      token: token.value.trim(),
      nickname: nickname.value.trim() || undefined,
      email: email.value.trim() || undefined,
      username: username.value.trim() || undefined,
      server_url: serverUrl.value.trim() || undefined,
    }
    emit('submit', req)
    resetForm()
  } finally {
    isSaving.value = false
  }
}

function onProviderChange() {
  // Set default server URL for well-known providers
  if (provider.value === 'github') {
    serverUrl.value = 'https://github.com'
  } else if (provider.value === 'gitlab') {
    serverUrl.value = 'https://gitlab.com'
  } else if (provider.value === 'bitbucket') {
    serverUrl.value = 'https://bitbucket.org'
  } else if (provider.value === 'gitea') {
    serverUrl.value = 'http://localhost:3000'
  } else {
    serverUrl.value = ''
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay">
      <div class="modal-content">
        <div class="modal-header">
          <h3>{{ t('settings.gitAddCredential') }}</h3>
          <button class="btn-close" @click="handleClose">✕</button>
        </div>

        <div class="modal-body">
          <!-- Provider selection -->
          <div class="form-field">
            <label>{{ t('settings.gitProviderLabel') }}</label>
            <select v-model="provider" @change="onProviderChange">
              <option v-for="p in providers" :key="p.value" :value="p.value">
                {{ p.label }}
              </option>
            </select>
          </div>

          <!-- Custom provider name (when 'custom' selected) -->
          <div v-if="provider === 'custom'" class="form-field">
            <label>{{ t('settings.gitCustomProviderLabel') }}</label>
            <input
              v-model="customProvider"
              type="text"
              :placeholder="t('settings.gitCustomProviderPlaceholder')"
            />
          </div>

          <!-- Auth type selection -->
          <div class="form-field">
            <label>{{ t('settings.gitAuthMethod') }}</label>
            <div class="auth-type-options">
              <label class="auth-option" :class="{ selected: authType === 'oauth' }">
                <input type="radio" v-model="authType" value="oauth" />
                <div class="auth-option-content">
                  <span class="auth-option-title">{{ t('settings.gitLinkAccount') }}</span>
                  <span class="auth-option-desc">{{ t('settings.gitLinkAccountDesc', { provider: providers.find(p => p.value === provider)?.label || customProvider }) }}</span>
                </div>
              </label>
              <label class="auth-option" :class="{ selected: authType === 'pat' }">
                <input type="radio" v-model="authType" value="pat" />
                <div class="auth-option-content">
                  <span class="auth-option-title">{{ t('settings.gitPersonalAccessToken') }}</span>
                  <span class="auth-option-desc">{{ t('settings.gitPersonalAccessTokenDesc') }}</span>
                </div>
              </label>
            </div>
          </div>

          <!-- Nickname -->
          <div class="form-field">
            <label>{{ t('settings.gitNicknameLabel') }}</label>
            <input
              v-model="nickname"
              type="text"
              :placeholder="t('settings.gitNicknamePlaceholder')"
            />
          </div>

          <!-- PAT specific fields -->
          <template v-if="authType === 'pat'">
            <div class="form-field">
              <label>{{ t('git.serverUrl') }}</label>
              <input
                v-model="serverUrl"
                type="text"
                :placeholder="t('git.serverUrlPlaceholder')"
              />
              <span class="field-hint">{{ t('settings.gitServerUrlHint') }}</span>
            </div>

            <div class="form-field">
              <label>{{ t('settings.gitUsernameLabel') }}</label>
              <input
                v-model="username"
                type="text"
                :placeholder="t('settings.gitUsernamePlaceholder')"
              />
            </div>

            <div class="form-field">
              <label>{{ t('settings.gitEmailLabel') }}</label>
              <input
                v-model="email"
                type="email"
                :placeholder="t('settings.gitEmailPlaceholder')"
              />
            </div>

            <div class="form-field">
              <label>{{ t('settings.gitTokenLabel') }}</label>
              <input
                v-model="token"
                type="password"
                :placeholder="t('git.tokenPlaceholder')"
              />
              <span class="field-hint">{{ t('settings.gitTokenHint') }}</span>
            </div>
          </template>
        </div>

        <div class="modal-footer">
          <button class="btn-cancel" @click="handleClose">{{ t('common.cancel') }}</button>
          <button
            class="btn-primary"
            :disabled="!isValid || isSaving"
            @click="handleSubmit"
          >
            {{ isSaving ? t('common.loading') : (authType === 'oauth' ? t('git.connect') : t('common.save')) }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-content {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 12px;
  width: 480px;
  max-width: 90vw;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: var(--shadow-float-lg);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 0;
}

.modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.btn-close {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 16px;
  cursor: pointer;
  padding: 4px;
}

.btn-close:hover {
  color: var(--text-primary);
}

.modal-body {
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-field label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.form-field input,
.form-field select {
  padding: 8px 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}

.form-field input:focus,
.form-field select:focus {
  border-color: var(--accent);
}

.field-hint {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.4;
}

.auth-type-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.auth-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s;
}

.auth-option:hover {
  border-color: var(--accent);
}

.auth-option.selected {
  border-color: var(--accent);
  background: var(--accent-subtler);
}

.auth-option input[type="radio"] {
  margin-top: 2px;
  accent-color: var(--accent);
}

.auth-option-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.auth-option-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.auth-option-desc {
  font-size: 12px;
  color: var(--text-muted);
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 0 24px 20px;
}

.btn-cancel {
  padding: 8px 16px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}

.btn-cancel:hover {
  border-color: var(--text-muted);
}

.btn-primary {
  padding: 8px 16px;
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary:not(:disabled):hover {
  filter: brightness(1.1);
}
</style>
