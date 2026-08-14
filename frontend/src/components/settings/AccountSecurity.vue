<script setup lang="ts">
/**
 * Account security panel — password change for the signed-in account.
 *
 * The backend revokes every refresh token on success, so the user is signed
 * out locally and sent back to the login page.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'

import { useAuthStore } from '@/stores/auth'
import { extractApiMessage } from '@/utils/notifyError'

const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const errorMsg = ref('')
const isSubmitting = ref(false)

const roleLabels: Record<string, string> = {
  admin: 'account.roleAdmin',
  developer: 'account.roleDeveloper',
  viewer: 'account.roleViewer',
}

const roleLabel = computed(() => {
  const role = authStore.user?.role
  return role ? t(roleLabels[role] ?? 'account.roleViewer') : ''
})

const canSubmit = computed(
  () =>
    Boolean(currentPassword.value && newPassword.value && confirmPassword.value) &&
    !isSubmitting.value
)

/** Local checks mirroring the backend password policy. */
function validate(): string {
  if (newPassword.value !== confirmPassword.value) return t('login.passwordMismatch')
  if (newPassword.value === currentPassword.value) return t('account.sameAsOld')
  return ''
}

async function handleSubmit() {
  const validationError = validate()
  if (validationError) {
    errorMsg.value = validationError
    return
  }

  errorMsg.value = ''
  isSubmitting.value = true
  try {
    await authStore.changePassword(currentPassword.value, newPassword.value)
    MessagePlugin.success(t('account.changeSuccess'))
    router.replace({ name: 'login' })
  } catch (error) {
    errorMsg.value = extractApiMessage(error)
    console.error('[account] Password change failed', error)
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <div class="account-security">
    <!-- Read-only identity summary of the active tenant context -->
    <div class="settings-card">
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-label">{{ t('login.username') }}</span>
        </div>
        <span class="setting-value">{{ authStore.user?.username }}</span>
      </div>
      <div class="setting-divider" />
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-label">{{ t('account.organization') }}</span>
        </div>
        <span class="setting-value">{{ authStore.user?.tenantName }}</span>
      </div>
      <div class="setting-divider" />
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-label">{{ t('account.role') }}</span>
        </div>
        <span class="role-badge">{{ roleLabel }}</span>
      </div>
    </div>

    <form class="settings-card password-form" @submit.prevent="handleSubmit">
      <div class="form-field">
        <label for="account-current">{{ t('account.currentPassword') }}</label>
        <input
          id="account-current"
          v-model="currentPassword"
          type="password"
          autocomplete="current-password"
        />
      </div>

      <div class="form-field">
        <label for="account-new">{{ t('account.newPassword') }}</label>
        <input
          id="account-new"
          v-model="newPassword"
          type="password"
          autocomplete="new-password"
          :placeholder="t('login.passwordPlaceholder')"
        />
      </div>

      <div class="form-field">
        <label for="account-confirm">{{ t('account.confirmNewPassword') }}</label>
        <input
          id="account-confirm"
          v-model="confirmPassword"
          type="password"
          autocomplete="new-password"
        />
      </div>

      <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>

      <div class="form-actions">
        <t-button theme="primary" type="submit" :disabled="!canSubmit" :loading="isSubmitting">
          {{ t('account.changePassword') }}
        </t-button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.account-security {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 560px;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 4px 18px;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 0;
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.setting-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.setting-value {
  font-size: 13px;
  color: var(--text-secondary);
}

.setting-divider {
  height: 1px;
  background: var(--border);
}

.role-badge {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--accent-subtler);
  color: var(--accent);
}

.password-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px 18px;
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

.form-field input {
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 12px;
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
  transition:
    border-color 0.16s ease,
    box-shadow 0.16s ease;
}

.form-field input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-subtler);
}

.error-msg {
  font-size: 12px;
  color: var(--error);
  background: var(--error-subtle);
  padding: 8px 10px;
  border-radius: 6px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
