<script setup lang="ts">
/**
 * Tenant member management.
 *
 * Lists the accounts of the active tenant and lets an authorized user add sub
 * accounts. Adding an existing username links that account without touching its
 * password; an unknown username creates a new account with an initial one.
 *
 * Roles come from the backend: they are database-defined and tenant-editable, so
 * nothing is hard-coded here. Built-in role codes get a localized label; custom
 * roles are displayed with the name stored by the tenant.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { MessagePlugin } from 'tdesign-vue-next'

import { usePermission } from '@/composables/usePermission'
import { useAuthStore, type TenantMember } from '@/stores/auth'
import { useRbacStore, type RoleDefinition } from '@/stores/rbac'
import { extractApiMessage, notifyError } from '@/utils/notifyError'
import { formatLocalTime } from '@/utils/timezone'

const { t } = useI18n()
const authStore = useAuthStore()
const rbacStore = useRbacStore()
const { can } = usePermission()

const members = ref<TenantMember[]>([])
const roles = ref<RoleDefinition[]>([])
const isLoading = ref(false)

const showAddDialog = ref(false)
const isSubmitting = ref(false)
const formError = ref('')
const form = ref<{ username: string; password: string; role: string }>({
  username: '',
  password: '',
  role: '',
})

const canRead = computed(() => can('tenant:member:read'))
const canWrite = computed(() => can('tenant:member:write'))

/** Localized labels for the built-in tenant roles. */
const builtinRoleLabels: Record<string, string> = {
  admin: 'account.roleAdmin',
  developer: 'account.roleDeveloper',
  viewer: 'account.roleViewer',
}

const roleOptions = computed(() =>
  roles.value.map((role) => ({ value: role.code, label: roleLabel(role.code) }))
)

/**
 * Display name of a role code: built-in roles are localized, custom ones use
 * the name the tenant gave them.
 */
function roleLabel(code: string): string {
  const key = builtinRoleLabels[code]
  if (key) return t(key)
  return roles.value.find((role) => role.code === code)?.name || code
}

/** Member timestamps arrive as UTC and are rendered in the user's timezone. */
function joinedAtLabel(joinedAt: string): string {
  return joinedAt ? formatLocalTime(joinedAt, 'YYYY-MM-DD HH:mm') : '-'
}

async function loadMembers() {
  isLoading.value = true
  try {
    const [memberList, roleList] = await Promise.all([
      authStore.listMembers(),
      rbacStore.listRoles('tenant'),
    ])
    members.value = memberList
    roles.value = roleList
  } catch (error) {
    notifyError(error)
  } finally {
    isLoading.value = false
  }
}

function openAddDialog() {
  form.value = { username: '', password: '', role: roles.value[0]?.code ?? '' }
  formError.value = ''
  showAddDialog.value = true
}

async function handleAddMember() {
  if (!form.value.username.trim()) {
    formError.value = t('login.usernameRequired')
    return
  }
  if (!form.value.role) {
    formError.value = t('members.roleRequired')
    return
  }

  formError.value = ''
  isSubmitting.value = true
  try {
    const result = await authStore.addMember({
      username: form.value.username.trim(),
      password: form.value.password,
      role: form.value.role,
    })
    MessagePlugin.success(result.userCreated ? t('members.addSuccess') : t('members.linkSuccess'))
    showAddDialog.value = false
    await loadMembers()
  } catch (error) {
    formError.value = extractApiMessage(error)
    console.error('[members] Failed to add a member', error)
  } finally {
    isSubmitting.value = false
  }
}

onMounted(async () => {
  // Permissions must be known before deciding whether to query the member list.
  await rbacStore.ensureIdentity().catch((error) => {
    console.error('[members] Failed to load identity permissions', error)
  })
  if (canRead.value) {
    await loadMembers()
  }
})
</script>

<template>
  <div class="member-management">
    <!-- The server enforces this too; the notice only explains the empty view. -->
    <p v-if="!canRead" class="admin-notice">{{ t('members.adminOnly') }}</p>

    <template v-else>
      <div class="toolbar">
        <span class="member-count">{{ members.length }}</span>
        <t-button v-if="canWrite" theme="primary" size="small" @click="openAddDialog">
          <template #icon><t-icon name="user-add" /></template>
          {{ t('members.add') }}
        </t-button>
      </div>

      <t-loading :loading="isLoading" size="small">
        <div class="member-table">
          <div class="table-head">
            <span>{{ t('members.username') }}</span>
            <span>{{ t('members.role') }}</span>
            <span>{{ t('members.joinedAt') }}</span>
          </div>

          <div v-for="member in members" :key="member.userId" class="table-row">
            <span class="cell-username">
              {{ member.username }}
              <span v-if="member.isOwner" class="owner-badge">{{ t('members.owner') }}</span>
            </span>
            <span class="cell-role">
              <span class="role-badge" :class="`role-${member.role}`">
                {{ roleLabel(member.role) }}
              </span>
            </span>
            <span class="cell-time">{{ joinedAtLabel(member.joinedAt) }}</span>
          </div>

          <p v-if="!isLoading && !members.length" class="empty-state">{{ t('members.empty') }}</p>
        </div>
      </t-loading>
    </template>

    <t-dialog
      v-model:visible="showAddDialog"
      :header="t('members.addTitle')"
      :confirm-btn="{ content: t('members.add'), loading: isSubmitting }"
      :cancel-btn="t('common.cancel')"
      width="440px"
      destroy-on-close
      @confirm="handleAddMember"
    >
      <div class="dialog-form">
        <div class="form-field">
          <label for="member-username">{{ t('members.username') }}</label>
          <t-input
            id="member-username"
            v-model="form.username"
            :placeholder="t('login.usernamePlaceholder')"
          />
          <p class="field-hint">{{ t('members.existingHint') }}</p>
        </div>

        <div class="form-field">
          <label for="member-password">{{ t('members.initialPassword') }}</label>
          <t-input
            id="member-password"
            v-model="form.password"
            type="password"
            :placeholder="t('login.passwordPlaceholder')"
          />
          <p class="field-hint">{{ t('members.initialPasswordHint') }}</p>
        </div>

        <div class="form-field">
          <label>{{ t('members.role') }}</label>
          <t-select v-model="form.role" :options="roleOptions" />
        </div>

        <p v-if="formError" class="error-msg">{{ formError }}</p>
      </div>
    </t-dialog>
  </div>
</template>

<style scoped>
.member-management {
  max-width: 720px;
}

.admin-notice {
  font-size: 13px;
  color: var(--text-muted);
  padding: 20px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 10px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.member-count {
  font-size: 12px;
  color: var(--text-muted);
}

.member-table {
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--bg-surface);
}

.table-head,
.table-row {
  display: grid;
  grid-template-columns: 2fr 1fr 1.2fr;
  gap: 12px;
  padding: 10px 16px;
  align-items: center;
}

.table-head {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}

.table-row {
  font-size: 13px;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-subtle);
  transition: background 0.14s ease;
}

.table-row:last-child {
  border-bottom: none;
}

.table-row:hover {
  background: var(--bg-hover);
}

.cell-username {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}

.owner-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--accent-subtler);
  color: var(--accent);
}

.role-badge {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--bg-secondary);
  color: var(--text-secondary);
}

.role-badge.role-admin {
  background: var(--accent-subtler);
  color: var(--accent);
}

.role-badge.role-developer {
  background: var(--info-subtle);
  color: var(--info);
}

.cell-time {
  font-size: 12px;
  color: var(--text-muted);
}

.empty-state {
  padding: 28px 0;
  text-align: center;
  font-size: 13px;
  color: var(--text-muted);
}

.dialog-form {
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

.field-hint {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.5;
}

.error-msg {
  font-size: 12px;
  color: var(--error);
  background: var(--error-subtle);
  padding: 8px 10px;
  border-radius: 6px;
}
</style>
