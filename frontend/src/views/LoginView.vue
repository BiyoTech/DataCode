<script setup lang="ts">
/**
 * Authentication entry page with three states:
 *   sign-in    — credentials form
 *   sign-up    — creates an organization and its administrator
 *   identity   — picks one of several tenant identities after sign-in
 */
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import IdentitySelect from '@/components/auth/IdentitySelect.vue'
import { useAuthStore } from '@/stores/auth'
import { extractApiMessage } from '@/utils/notifyError'

type FormMode = 'signIn' | 'signUp'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const mode = ref<FormMode>('signIn')
const username = ref('')
const password = ref('')
const confirmPassword = ref('')
const organization = ref('')
const errorMsg = ref('')
const isLoading = ref(false)
const pendingTenantId = ref('')

const isRegister = computed(() => mode.value === 'signUp')
const memberships = computed(() => authStore.pendingSelection?.memberships ?? [])
/** Identity picker replaces the form once login returns several identities. */
const showIdentityPicker = computed(() => authStore.requiresTenantSelection)

function switchMode(next: FormMode) {
  if (mode.value === next) return
  mode.value = next
  errorMsg.value = ''
  confirmPassword.value = ''
  organization.value = ''
}

/** Client-side checks that avoid a pointless round-trip. */
function validate(): string {
  if (!username.value.trim()) return t('login.usernameRequired')
  if (!password.value) return t('login.passwordRequired')
  if (isRegister.value) {
    if (!organization.value.trim()) return t('login.organizationRequired')
    if (password.value !== confirmPassword.value) return t('login.passwordMismatch')
  }
  return ''
}

/** Navigate to the originally requested page, defaulting to the home route. */
async function enterApp() {
  const redirect = route.query.redirect
  const target = typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/'
  await nextTick()
  router.replace(target)
}

async function handleSubmit() {
  const validationError = validate()
  if (validationError) {
    errorMsg.value = validationError
    return
  }

  errorMsg.value = ''
  isLoading.value = true
  try {
    if (isRegister.value) {
      await authStore.register(username.value, password.value, organization.value)
      await enterApp()
      return
    }

    const result = await authStore.login(username.value, password.value)
    // Multiple identities: stay on the page and render the picker.
    if (!result.requiresTenantSelection) {
      await enterApp()
    }
  } catch (error) {
    errorMsg.value = extractApiMessage(error, t('login.invalidCredentials'))
    console.error('[login] Authentication failed', error)
  } finally {
    isLoading.value = false
  }
}

async function handleSelectIdentity(tenantId: string) {
  errorMsg.value = ''
  pendingTenantId.value = tenantId
  try {
    await authStore.selectTenant(tenantId)
    await enterApp()
  } catch (error) {
    errorMsg.value = extractApiMessage(error)
    console.error('[login] Identity selection failed', error)
  } finally {
    pendingTenantId.value = ''
  }
}

/** Abandon the pending selection and return to the credentials form. */
function backToSignIn() {
  authStore.cancelTenantSelection()
  password.value = ''
  errorMsg.value = ''
}
</script>

<template>
  <div class="login-page">
    <div class="login-aurora" aria-hidden="true"></div>

    <div class="login-card" :class="{ 'is-wide': showIdentityPicker }">
      <div class="login-header">
        <h1 class="app-logo">DataCode</h1>
        <p class="app-tagline">{{ t('login.tagline') }}</p>
      </div>

      <!-- Identity selection (post sign-in) -->
      <template v-if="showIdentityPicker">
        <div class="picker-header">
          <h2 class="picker-title">{{ t('identity.title') }}</h2>
          <p class="picker-subtitle">
            {{ t('identity.subtitle', { count: memberships.length }) }}
          </p>
        </div>

        <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>

        <IdentitySelect
          :memberships="memberships"
          :pending-tenant-id="pendingTenantId"
          :disabled="Boolean(pendingTenantId)"
          @select="handleSelectIdentity"
        />

        <button type="button" class="text-link back-link" @click="backToSignIn">
          {{ t('identity.back') }}
        </button>
      </template>

      <!-- Sign in / Sign up -->
      <template v-else>
        <div class="mode-switch" role="tablist">
          <button
            type="button"
            role="tab"
            class="mode-tab"
            :class="{ 'is-active': !isRegister }"
            :aria-selected="!isRegister"
            @click="switchMode('signIn')"
          >
            {{ t('login.signIn') }}
          </button>
          <button
            type="button"
            role="tab"
            class="mode-tab"
            :class="{ 'is-active': isRegister }"
            :aria-selected="isRegister"
            @click="switchMode('signUp')"
          >
            {{ t('login.signUp') }}
          </button>
        </div>

        <form class="login-form" @submit.prevent="handleSubmit">
          <div class="form-group">
            <label for="login-username">{{ t('login.username') }}</label>
            <input
              id="login-username"
              v-model="username"
              type="text"
              :placeholder="isRegister ? t('login.usernamePlaceholder') : t('login.username')"
              autocomplete="username"
            />
          </div>

          <div class="form-group">
            <label for="login-password">{{ t('login.password') }}</label>
            <input
              id="login-password"
              v-model="password"
              type="password"
              :placeholder="isRegister ? t('login.passwordPlaceholder') : t('login.password')"
              :autocomplete="isRegister ? 'new-password' : 'current-password'"
            />
          </div>

          <template v-if="isRegister">
            <div class="form-group">
              <label for="login-confirm">{{ t('login.confirmPassword') }}</label>
              <input
                id="login-confirm"
                v-model="confirmPassword"
                type="password"
                :placeholder="t('login.confirmPassword')"
                autocomplete="new-password"
              />
            </div>

            <div class="form-group">
              <label for="login-org">{{ t('login.organization') }}</label>
              <input
                id="login-org"
                v-model="organization"
                type="text"
                :placeholder="t('login.organizationPlaceholder')"
                autocomplete="organization"
              />
              <p class="field-hint">{{ t('login.organizationHint') }}</p>
            </div>
          </template>

          <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>

          <button type="submit" class="login-btn" :disabled="isLoading">
            {{
              isLoading
                ? t('common.loading')
                : isRegister
                  ? t('login.submitRegister')
                  : t('login.submit')
            }}
          </button>
        </form>

        <p class="login-footer">
          <template v-if="isRegister">
            {{ t('login.hasAccount') }}
            <button type="button" class="text-link" @click="switchMode('signIn')">
              {{ t('login.loginNow') }}
            </button>
          </template>
          <template v-else>
            {{ t('login.noAccount') }}
            <button type="button" class="text-link" @click="switchMode('signUp')">
              {{ t('login.registerNow') }}
            </button>
          </template>
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 32px 16px;
  background: var(--bg-primary);
  overflow: hidden;
}

/* Subtle brand-tinted glow behind the card. */
.login-aurora {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(60% 45% at 50% 0%, var(--accent-subtler) 0%, transparent 70%),
    radial-gradient(40% 35% at 85% 90%, var(--accent-subtler) 0%, transparent 70%);
  opacity: 0.9;
  pointer-events: none;
}

.login-card {
  position: relative;
  width: 380px;
  max-width: 100%;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 36px 32px;
  box-shadow: var(--shadow-float-lg);
  animation: card-enter 0.32s ease both;
  transition: width 0.24s ease;
}

.login-card.is-wide {
  width: 440px;
}

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.login-header {
  text-align: center;
  margin-bottom: 24px;
}

.app-logo {
  font-size: 24px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 4px;
}

.app-tagline {
  font-size: 13px;
  color: var(--text-muted);
}

.mode-switch {
  display: flex;
  gap: 4px;
  padding: 4px;
  margin-bottom: 20px;
  background: var(--bg-secondary);
  border-radius: 10px;
}

.mode-tab {
  flex: 1;
  padding: 8px 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  background: transparent;
  border: none;
  border-radius: 7px;
  cursor: pointer;
  transition:
    background 0.18s ease,
    color 0.18s ease;
}

.mode-tab:hover:not(.is-active) {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.mode-tab.is-active {
  color: var(--accent-foreground);
  background: var(--accent);
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
  animation: form-fade 0.24s ease both;
}

@keyframes form-fade {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 500;
}

.form-group input {
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
  transition:
    border-color 0.16s ease,
    box-shadow 0.16s ease;
}

.form-group input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-subtler);
}

.field-hint {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.5;
}

.error-msg {
  font-size: 12px;
  color: var(--error);
  padding: 8px 10px;
  background: var(--error-subtle);
  border-radius: 6px;
}

.login-btn {
  background: var(--accent);
  color: var(--accent-foreground);
  border: none;
  border-radius: 8px;
  padding: 11px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  margin-top: 4px;
  transition:
    background 0.16s ease,
    transform 0.16s ease;
}

.login-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.login-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
}

.login-footer {
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 18px;
}

.text-link {
  background: none;
  border: none;
  padding: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--accent);
  cursor: pointer;
}

.text-link:hover {
  text-decoration: underline;
}

.picker-header {
  margin-bottom: 18px;
}

.picker-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 4px;
}

.picker-subtitle {
  font-size: 12px;
  color: var(--text-muted);
}

.back-link {
  display: block;
  margin: 18px auto 0;
}
</style>
