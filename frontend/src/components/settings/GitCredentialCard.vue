<script setup lang="ts">
/**
 * GitCredentialCard - displays a single Git credential with actions.
 * Shows provider icon, nickname, auth type badge, default indicator, and action menu.
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { GitCredential } from '@/types/git-credentials'

const { t } = useI18n()

const props = defineProps<{
  credential: GitCredential
}>()

const emit = defineEmits<{
  (e: 'edit', id: string): void
  (e: 'delete', id: string): void
  (e: 'setDefault', id: string): void
}>()

const showMenu = ref(false)

const providerIcons: Record<string, string> = {
  github: '🐙',
  gitlab: '🦊',
  bitbucket: '🪣',
  gitea: '🍵',
}

const providerLabels: Record<string, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
  bitbucket: 'Bitbucket',
  gitea: 'Gitea',
}

function toggleMenu() {
  showMenu.value = !showMenu.value
}

function handleAction(action: string) {
  showMenu.value = false
  if (action === 'edit') emit('edit', props.credential.id)
  else if (action === 'delete') emit('delete', props.credential.id)
  else if (action === 'setDefault') emit('setDefault', props.credential.id)
}
</script>

<template>
  <div class="credential-card">
    <div class="card-left">
      <span class="provider-icon">{{ providerIcons[credential.provider] || '🔗' }}</span>
      <div class="card-info">
        <div class="card-title-row">
          <span class="card-nickname">{{ credential.nickname }}</span>
          <span v-if="credential.is_default" class="default-badge">
            {{ t('settings.gitDefaultBadge') }}
          </span>
          <span class="auth-type-badge" :class="credential.auth_type">
            {{ credential.auth_type === 'oauth' ? 'OAuth' : 'PAT' }}
          </span>
        </div>
        <div class="card-meta">
          <span v-if="credential.email" class="meta-item">{{ credential.email }}</span>
          <span v-if="credential.server_url" class="meta-item">{{ credential.server_url }}</span>
          <span class="meta-item">{{ providerLabels[credential.provider] || credential.provider }}</span>
        </div>
      </div>
    </div>

    <div class="card-actions">
      <button
        v-if="credential.auth_type === 'pat'"
        class="btn-action"
        @click="emit('edit', credential.id)"
      >
        {{ t('common.edit') }}
      </button>
      <div class="menu-container">
        <button class="btn-menu" @click="toggleMenu">⋯</button>
        <div v-if="showMenu" class="dropdown-menu" @mouseleave="showMenu = false">
          <button
            v-if="!credential.is_default"
            class="menu-item"
            @click="handleAction('setDefault')"
          >
            {{ t('settings.gitSetAsDefault') }}
          </button>
          <button class="menu-item danger" @click="handleAction('delete')">
            {{ t('common.delete') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.credential-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
}

.credential-card:last-child {
  border-bottom: none;
}

.card-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.provider-icon {
  font-size: 24px;
  flex-shrink: 0;
}

.card-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.card-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.card-nickname {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}

.default-badge {
  background: var(--accent-dim);
  color: var(--accent);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.auth-type-badge {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
}

.auth-type-badge.pat {
  background: var(--warning-subtle);
  color: var(--warning);
}

.auth-type-badge.oauth {
  background: var(--success-subtle);
  color: var(--success);
}

.card-meta {
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: var(--text-muted);
}

.meta-item {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.btn-action {
  padding: 5px 12px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.btn-action:hover {
  border-color: var(--accent);
  color: var(--text-primary);
}

.menu-container {
  position: relative;
}

.btn-menu {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
}

.btn-menu:hover {
  background: var(--bg-hover);
}

.dropdown-menu {
  position: absolute;
  right: 0;
  top: 100%;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float);
  min-width: 140px;
  z-index: 50;
  padding: 4px;
}

.menu-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  background: none;
  border: none;
  border-radius: 4px;
  font-size: 13px;
  color: var(--text-primary);
  cursor: pointer;
}

.menu-item:hover {
  background: var(--bg-hover);
}

.menu-item.danger {
  color: var(--error);
}

.menu-item.danger:hover {
  background: var(--error-subtle);
}
</style>
