<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const authStore = useAuthStore()

const username = ref('admin')
const password = ref('')
const errorMsg = ref('')
const isLoading = ref(false)

async function handleLogin() {
  errorMsg.value = ''
  isLoading.value = true

  try {
    const success = await authStore.login(username.value, password.value)
    if (!success) {
      errorMsg.value = t('login.invalidCredentials')
    }
  } finally {
    isLoading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <h1 class="app-logo">AgenticData</h1>
        <p class="app-tagline">AI Native DataOps Platform</p>
      </div>

      <form @submit.prevent="handleLogin" class="login-form">
        <div class="form-group">
          <label>{{ t('login.username') }}</label>
          <input
            v-model="username"
            type="text"
            :placeholder="t('login.username')"
            autocomplete="username"
          />
        </div>

        <div class="form-group">
          <label>{{ t('login.password') }}</label>
          <input
            v-model="password"
            type="password"
            :placeholder="t('login.password')"
            autocomplete="current-password"
          />
        </div>

        <p v-if="errorMsg" class="error-msg">{{ errorMsg }}</p>

        <button type="submit" class="login-btn" :disabled="isLoading">
          {{ isLoading ? t('common.loading') : t('login.submit') }}
        </button>
      </form>

      <p class="login-hint">{{ t('login.hint') }}</p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--bg-primary);
}

.login-card {
  width: 360px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 40px 32px;
}

.login-header {
  text-align: center;
  margin-bottom: 32px;
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

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
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
}

.form-group input:focus {
  border-color: var(--accent);
}

.error-msg {
  font-size: 12px;
  color: var(--error);
}

.login-btn {
  background: var(--accent);
  color: var(--accent-foreground);
  border: none;
  border-radius: 8px;
  padding: 10px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  margin-top: 8px;
}

.login-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.login-btn:hover:not(:disabled) {
  opacity: 0.9;
}

.login-hint {
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 16px;
}
</style>
