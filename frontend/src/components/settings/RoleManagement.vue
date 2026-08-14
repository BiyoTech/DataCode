<script setup lang="ts">
/**
 * Role management.
 *
 * Roles and their permission composition live in the database and are editable
 * per tenant — including the built-in ones, whose name and permission set may be
 * adjusted while their code stays immutable so existing bindings keep working.
 *
 * The permission catalog on the right is read-only: it is declared in backend
 * code and synchronized on startup, so a permission can never be invented here.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'

import { usePermission } from '@/composables/usePermission'
import {
  useRbacStore,
  type PermissionDef,
  type RbacScope,
  type RoleDefinition,
} from '@/stores/rbac'
import { extractApiMessage, notifyError } from '@/utils/notifyError'

const { t } = useI18n()
const rbacStore = useRbacStore()
const { can } = usePermission()

const scope = ref<RbacScope>('tenant')
const roles = ref<RoleDefinition[]>([])
const selectedRoleId = ref('')
const isLoading = ref(false)
const isSaving = ref(false)
const formError = ref('')

/** Draft of the currently edited role; applied only on save. */
const draft = ref<{ name: string; description: string; permissions: string[] }>({
  name: '',
  description: '',
  permissions: [],
})

const showCreateDialog = ref(false)
const createForm = ref<{ code: string; name: string; description: string }>({
  code: '',
  name: '',
  description: '',
})

const canRead = computed(() => can('tenant:role:read'))
const canWrite = computed(() => can('tenant:role:write'))

/** Localized labels for built-in role codes; custom roles use their own name. */
const builtinRoleLabels: Record<string, string> = {
  admin: 'account.roleAdmin',
  developer: 'account.roleDeveloper',
  viewer: 'account.roleViewer',
  workspace_owner: 'workspace.roleOwner',
  engineer: 'workspace.roleEngineer',
  analyst: 'workspace.roleAnalyst',
}

const selectedRole = computed(() => roles.value.find((role) => role.id === selectedRoleId.value))

/** Catalog entries of the active scope, grouped by resource for the matrix. */
const permissionGroups = computed<{ resource: string; permissions: PermissionDef[] }[]>(() => {
  const grouped = new Map<string, PermissionDef[]>()
  for (const permission of rbacStore.catalogByScope[scope.value]) {
    const bucket = grouped.get(permission.resource) ?? []
    bucket.push(permission)
    grouped.set(permission.resource, bucket)
  }
  return [...grouped.entries()].map(([resource, permissions]) => ({ resource, permissions }))
})

/** True when the draft differs from the persisted role, to highlight the save. */
const isDirty = computed(() => {
  const role = selectedRole.value
  if (!role) return false
  if (draft.value.name !== role.name || draft.value.description !== role.description) return true
  if (draft.value.permissions.length !== role.permissions.length) return true
  return draft.value.permissions.some((code) => !role.permissions.includes(code))
})

function displayName(role: RoleDefinition): string {
  const key = builtinRoleLabels[role.code]
  return role.isBuiltin && key ? t(key) : role.name
}

function isGranted(code: string): boolean {
  return draft.value.permissions.includes(code)
}

function togglePermission(code: string, granted: boolean): void {
  const current = new Set(draft.value.permissions)
  if (granted) current.add(code)
  else current.delete(code)
  draft.value.permissions = [...current]
}

function selectRole(roleId: string): void {
  selectedRoleId.value = roleId
  formError.value = ''
  const role = roles.value.find((item) => item.id === roleId)
  draft.value = {
    name: role?.name ?? '',
    description: role?.description ?? '',
    permissions: [...(role?.permissions ?? [])],
  }
}

async function loadRoles(): Promise<void> {
  isLoading.value = true
  try {
    // The catalog is immutable, so it is fetched once per session.
    await rbacStore.ensureCatalog()
    roles.value = await rbacStore.listRoles(scope.value)
    selectRole(roles.value[0]?.id ?? '')
  } catch (error) {
    notifyError(error)
  } finally {
    isLoading.value = false
  }
}

async function switchScope(next: RbacScope): Promise<void> {
  if (scope.value === next) return
  scope.value = next
  await loadRoles()
}

async function saveRole(): Promise<void> {
  const role = selectedRole.value
  if (!role || isSaving.value) return
  if (!draft.value.name.trim()) {
    formError.value = t('rbac.nameRequired')
    return
  }

  formError.value = ''
  isSaving.value = true
  try {
    await rbacStore.updateRole(role.id, {
      name: draft.value.name.trim(),
      description: draft.value.description,
      permissions: draft.value.permissions,
    })
    MessagePlugin.success(t('rbac.saveSuccess'))
    // The active identity's own capabilities may have just changed.
    await rbacStore.ensureIdentity(true)
    await loadRoles()
    selectRole(role.id)
  } catch (error) {
    formError.value = extractApiMessage(error)
    console.error('[rbac] Failed to update the role', error)
  } finally {
    isSaving.value = false
  }
}

function openCreateDialog(): void {
  createForm.value = { code: '', name: '', description: '' }
  formError.value = ''
  showCreateDialog.value = true
}

async function createRole(): Promise<void> {
  if (!createForm.value.code.trim() || !createForm.value.name.trim()) {
    formError.value = t('rbac.codeAndNameRequired')
    return
  }

  isSaving.value = true
  try {
    const created = await rbacStore.createRole({
      scope: scope.value,
      code: createForm.value.code.trim().toLowerCase(),
      name: createForm.value.name.trim(),
      description: createForm.value.description,
      // A new role starts with no permission: grants are made explicitly.
      permissions: [],
    })
    MessagePlugin.success(t('rbac.createSuccess'))
    showCreateDialog.value = false
    await loadRoles()
    selectRole(created.id)
  } catch (error) {
    formError.value = extractApiMessage(error)
    console.error('[rbac] Failed to create the role', error)
  } finally {
    isSaving.value = false
  }
}

function confirmDelete(role: RoleDefinition): void {
  const dialog = DialogPlugin.confirm({
    header: t('rbac.deleteTitle'),
    body: t('rbac.deleteConfirm', { name: displayName(role) }),
    confirmBtn: { content: t('common.confirm'), theme: 'danger' },
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      try {
        await rbacStore.deleteRole(role.id)
        MessagePlugin.success(t('rbac.deleteSuccess'))
        await loadRoles()
      } catch (error) {
        notifyError(error)
      } finally {
        dialog.destroy()
      }
    },
  })
}

onMounted(async () => {
  await rbacStore.ensureIdentity().catch((error) => {
    console.error('[rbac] Failed to load identity permissions', error)
  })
  if (canRead.value) {
    await loadRoles()
  }
})
</script>

<template>
  <div class="role-management">
    <!-- The server enforces this too; the notice only explains the empty view. -->
    <p v-if="!canRead" class="access-notice">{{ t('rbac.readOnlyNotice') }}</p>

    <template v-else>
      <div class="toolbar">
        <div class="scope-tabs">
          <button
            :class="['scope-tab', { active: scope === 'tenant' }]"
            @click="switchScope('tenant')"
          >
            {{ t('rbac.scopeTenant') }}
          </button>
          <button
            :class="['scope-tab', { active: scope === 'workspace' }]"
            @click="switchScope('workspace')"
          >
            {{ t('rbac.scopeWorkspace') }}
          </button>
        </div>
        <t-button v-if="canWrite" theme="primary" size="small" @click="openCreateDialog">
          <template #icon><t-icon name="add" /></template>
          {{ t('rbac.createRole') }}
        </t-button>
      </div>

      <t-loading :loading="isLoading" size="small">
        <div class="role-layout">
          <!-- Role list -->
          <ul class="role-list">
            <li
              v-for="role in roles"
              :key="role.id"
              :class="['role-item', { active: role.id === selectedRoleId }]"
              @click="selectRole(role.id)"
            >
              <div class="role-item-head">
                <span class="role-name">{{ displayName(role) }}</span>
                <span :class="['role-tag', role.isBuiltin ? 'builtin' : 'custom']">
                  {{ role.isBuiltin ? t('rbac.builtin') : t('rbac.custom') }}
                </span>
              </div>
              <div class="role-item-meta">
                <span class="role-code">{{ role.code }}</span>
                <span>{{ t('rbac.memberCount', { count: role.memberCount }) }}</span>
              </div>
            </li>
            <li v-if="!roles.length" class="empty-state">{{ t('rbac.noRoles') }}</li>
          </ul>

          <!-- Permission matrix -->
          <div v-if="selectedRole" class="role-detail">
            <div class="detail-head">
              <div class="detail-fields">
                <div class="form-field">
                  <label for="role-name">{{ t('rbac.roleName') }}</label>
                  <t-input id="role-name" v-model="draft.name" :disabled="!canWrite" />
                </div>
                <div class="form-field">
                  <label for="role-desc">{{ t('rbac.roleDescription') }}</label>
                  <t-input id="role-desc" v-model="draft.description" :disabled="!canWrite" />
                </div>
              </div>
              <div class="detail-actions">
                <t-button
                  v-if="canWrite"
                  theme="primary"
                  size="small"
                  :loading="isSaving"
                  :disabled="!isDirty"
                  @click="saveRole"
                >
                  {{ t('common.save') }}
                </t-button>
                <t-button
                  v-if="canWrite && !selectedRole.isBuiltin"
                  theme="danger"
                  variant="outline"
                  size="small"
                  @click="confirmDelete(selectedRole)"
                >
                  {{ t('common.delete') }}
                </t-button>
              </div>
            </div>

            <p class="matrix-hint">{{ t('rbac.matrixHint') }}</p>

            <div class="permission-matrix">
              <div v-for="group in permissionGroups" :key="group.resource" class="permission-group">
                <h4 class="group-title">{{ group.resource }}</h4>
                <label
                  v-for="permission in group.permissions"
                  :key="permission.code"
                  class="permission-row"
                >
                  <t-checkbox
                    :checked="isGranted(permission.code)"
                    :disabled="!canWrite"
                    @change="(value: boolean) => togglePermission(permission.code, value)"
                  />
                  <span class="permission-text">
                    <span class="permission-desc">{{ permission.description }}</span>
                    <span class="permission-code">{{ permission.code }}</span>
                  </span>
                </label>
              </div>
            </div>

            <p v-if="formError" class="error-msg">{{ formError }}</p>
          </div>
        </div>
      </t-loading>
    </template>

    <t-dialog
      v-model:visible="showCreateDialog"
      :header="t('rbac.createRole')"
      :confirm-btn="{ content: t('common.create'), loading: isSaving }"
      :cancel-btn="t('common.cancel')"
      width="440px"
      destroy-on-close
      @confirm="createRole"
    >
      <div class="dialog-form">
        <div class="form-field">
          <label for="new-role-code">{{ t('rbac.roleCode') }}</label>
          <t-input id="new-role-code" v-model="createForm.code" placeholder="data_steward" />
          <p class="field-hint">{{ t('rbac.roleCodeHint') }}</p>
        </div>
        <div class="form-field">
          <label for="new-role-name">{{ t('rbac.roleName') }}</label>
          <t-input id="new-role-name" v-model="createForm.name" />
        </div>
        <div class="form-field">
          <label for="new-role-desc">{{ t('rbac.roleDescription') }}</label>
          <t-input id="new-role-desc" v-model="createForm.description" />
        </div>
        <p v-if="formError" class="error-msg">{{ formError }}</p>
      </div>
    </t-dialog>
  </div>
</template>

<style scoped>
.role-management {
  max-width: 960px;
}

.access-notice {
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

.scope-tabs {
  display: flex;
  gap: 4px;
  padding: 2px;
  background: var(--bg-secondary);
  border-radius: 8px;
}

.scope-tab {
  background: none;
  border: none;
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.scope-tab.active {
  background: var(--bg-surface);
  color: var(--text-primary);
  font-weight: 600;
}

.role-layout {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  align-items: start;
}

.role-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--bg-surface);
}

.role-item {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  cursor: pointer;
  transition: background 0.14s ease;
}

.role-item:last-child {
  border-bottom: none;
}

.role-item:hover {
  background: var(--bg-hover);
}

.role-item.active {
  background: var(--accent-subtler);
}

.role-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.role-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.role-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
}

.role-tag.builtin {
  background: var(--info-subtle);
  color: var(--info);
}

.role-tag.custom {
  background: var(--bg-secondary);
  color: var(--text-secondary);
}

.role-item-meta {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-muted);
}

.role-code {
  font-family: var(--font-mono, monospace);
}

.role-detail {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-surface);
  padding: 16px;
}

.detail-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.detail-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  flex: 1;
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.matrix-hint {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 12px;
}

.permission-matrix {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}

.permission-group {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 10px 12px;
}

.group-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin-bottom: 8px;
}

.permission-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 0;
  cursor: pointer;
}

.permission-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.permission-desc {
  font-size: 12px;
  color: var(--text-primary);
}

.permission-code {
  font-size: 10px;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}

.empty-state {
  padding: 24px 0;
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
  margin-top: 12px;
  font-size: 12px;
  color: var(--error);
  background: var(--error-subtle);
  padding: 8px 10px;
  border-radius: 6px;
}
</style>
