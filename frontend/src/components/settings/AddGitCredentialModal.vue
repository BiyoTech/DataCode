<script setup lang="ts">
/**
 * AddGitCredentialModal - modal for adding a new Git credential.
 *
 * Only the personal-access-token flow can be submitted. Account linking
 * (OAuth) is reserved for GitHub and shown as a disabled, upcoming option, so
 * the product never persists a token-less credential that would fail on every
 * later Git operation.
 *
 * A credential is keyed by HOST (remote operations resolve credentials by the
 * remote URL's host), so the form blocks a second credential for a host that
 * is already configured.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  OAUTH_CAPABLE_PROVIDERS,
  type AuthType,
  type CreateGitCredentialRequest,
} from '@/types/git-credentials'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()

defineProps<{
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
const serverUrl = ref('https://github.com')
const isSaving = ref(false)

const providers: { value: string; label: string }[] = [
  { value: 'github', label: 'GitHub' },
  { value: 'gitlab', label: 'GitLab' },
  { value: 'bitbucket', label: 'Bitbucket' },
  { value: 'gitea', label: 'Gitea' },
  { value: 'custom', label: 'Custom' },
]

/** Public host a provider serves when no explicit server URL is given. */
const PROVIDER_DEFAULT_HOSTS: Record<string, string> = {
  github: 'github.com',
  gitlab: 'gitlab.com',
  bitbucket: 'bitbucket.org',
}

/**
 * Mirror of the backend's `credential_host()`: an explicit server URL wins,
 * otherwise the provider's public host. Returns '' when the URL is unusable.
 */
function hostOf(providerName: string, serverUrlValue: string): string {
  const raw = (serverUrlValue || '').trim()
  if (raw) {
    try {
      return new URL(raw.includes('://') ? raw : `https://${raw}`).hostname.toLowerCase()
    } catch {
      return ''
    }
  }
  return PROVIDER_DEFAULT_HOSTS[(providerName || '').toLowerCase()] || ''
}

const providerLabel = computed(
  () => providers.find((p) => p.value === provider.value)?.label || customProvider.value
)

const effectiveProvider = computed(() =>
  provider.value === 'custom' ? customProvider.value.trim().toLowerCase() : provider.value
)

/** Whether account linking is planned for the selected provider. */
const supportsAccountLink = computed(() =>
  OAUTH_CAPABLE_PROVIDERS.includes(provider.value as never)
)

const derivedHost = computed(() => hostOf(effectiveProvider.value, serverUrl.value))

/** Credential already registered for the same host, if any. */
const hostConflict = computed(() => {
  if (!derivedHost.value) return null
  return (
    workspaceStore.gitCredentials.find(
      (cred) => (cred.host || hostOf(cred.provider, cred.server_url)) === derivedHost.value
    ) || null
  )
})

const isValid = computed(() => {
  if (authType.value !== 'pat') return false
  if (provider.value === 'custom' && !customProvider.value.trim()) return false
  if (!token.value.trim()) return false
  if (!derivedHost.value) return false
  return hostConflict.value === null
})

function defaultServerUrl(providerName: string): string {
  const preset: Record<string, string> = {
    github: 'https://github.com',
    gitlab: 'https://gitlab.com',
    bitbucket: 'https://bitbucket.org',
    gitea: 'http://localhost:3000',
  }
  return preset[providerName] || ''
}

function resetForm() {
  provider.value = 'github'
  customProvider.value = ''
  authType.value = 'pat'
  nickname.value = ''
  token.value = ''
  email.value = ''
  username.value = ''
  serverUrl.value = defaultServerUrl('github')
  isSaving.value = false
}

function handleClose() {
  resetForm()
  emit('close')
}

function handleSubmit() {
  if (!isValid.value || isSaving.value) return
  isSaving.value = true
  try {
    emit('submit', {
      provider: effectiveProvider.value,
      auth_type: 'pat',
      token: token.value.trim(),
      nickname: nickname.value.trim() || undefined,
      email: email.value.trim() || undefined,
      username: username.value.trim() || undefined,
      server_url: serverUrl.value.trim(),
    })
    resetForm()
  } finally {
    isSaving.value = false
  }
}

// Switching provider must never leave a stale auth type behind: the OAuth
// option only exists for GitHub, and leaving it selected for another provider
// would hide the token fields and submit an empty credential.
watch(provider, (next) => {
  authType.value = 'pat'
  serverUrl.value = defaultServerUrl(next)
})
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
            <select v-model="provider">
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

          <!-- Auth type: account linking is reserved for GitHub and not enabled yet -->
          <div class="form-field">
            <label>{{ t('settings.gitAuthMethod') }}</label>
            <div class="auth-type-options">
              <label
                v-if="supportsAccountLink"
                class="auth-option disabled"
                :title="t('settings.gitLinkAccountReservedHint')"
              >
                <input type="radio" value="oauth" disabled />
                <div class="auth-option-content">
                  <span class="auth-option-title">
                    {{ t('settings.gitLinkAccount') }}
                    <span class="badge-soon">{{ t('settings.gitLinkAccountComingSoon') }}</span>
                  </span>
                  <span class="auth-option-desc">
                    {{ t('settings.gitLinkAccountDesc', { provider: providerLabel }) }}
                  </span>
                </div>
              </label>
              <label class="auth-option selected">
                <input type="radio" v-model="authType" value="pat" />
                <div class="auth-option-content">
                  <span class="auth-option-title">{{ t('settings.gitPersonalAccessToken') }}</span>
                  <span class="auth-option-desc">{{ t('settings.gitPersonalAccessTokenDesc') }}</span>
                </div>
              </label>
            </div>
            <span v-if="!supportsAccountLink" class="field-hint">
              {{ t('settings.gitPatOnlyHint', { provider: providerLabel }) }}
            </span>
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

          <div class="form-field">
            <label>{{ t('git.serverUrl') }}</label>
            <input
              v-model="serverUrl"
              type="text"
              :placeholder="t('git.serverUrlPlaceholder')"
            />
            <span v-if="derivedHost" class="field-hint">
              {{ t('settings.gitHostLabel') }}: {{ derivedHost }}
            </span>
            <span v-else class="field-error">{{ t('settings.gitHostRequired') }}</span>
            <span v-if="hostConflict" class="field-error">
              {{
                t('settings.gitHostConflict', {
                  host: derivedHost,
                  nickname: hostConflict.nickname,
                })
              }}
            </span>
            <span v-else class="field-hint">{{ t('settings.gitServerUrlHint') }}</span>
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
        </div>

        <div class="modal-footer">
          <button class="btn-cancel" @click="handleClose">{{ t('common.cancel') }}</button>
          <button
            class="btn-primary"
            :disabled="!isValid || isSaving"
            @click="handleSubmit"
          >
            {{ isSaving ? t('common.loading') : t('common.save') }}
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

.field-error {
  font-size: 11px;
  color: var(--error);
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

/* Reserved capability: visible so users know it is planned, not selectable. */
.auth-option.disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.auth-option.disabled:hover {
  border-color: var(--border);
}

.badge-soon {
  margin-left: 6px;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 10px;
  font-weight: 500;
  color: var(--text-muted);
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
