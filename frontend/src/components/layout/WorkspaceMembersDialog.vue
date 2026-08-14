<script setup lang="ts">
/**
 * Workspace member management dialog.
 *
 * Lets an authorized member grant tenant sub-accounts access to a workspace:
 * pick a candidate from the tenant directory, assign a workspace role, or revoke
 * an existing member.
 *
 * The assignable roles are fetched from the backend (they are database-defined
 * and tenant-editable, and the implicit owner role is never offered). All
 * authorization is enforced server-side; the UI only hides what it may not do.
 */
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePermission } from '@/composables/usePermission'
import { useRbacStore, type RoleDefinition } from '@/stores/rbac'
import { useWorkspaceStore, type WorkspaceMember, type TenantMemberCandidate } from '@/stores/workspace'
import { notifyError } from '@/utils/notifyError'

const props = defineProps<{ workspaceId: string; workspaceName: string }>()
const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const rbacStore = useRbacStore()
const { canInWorkspace } = usePermission()

const members = ref<WorkspaceMember[]>([])
const candidates = ref<TenantMemberCandidate[]>([])
const roles = ref<RoleDefinition[]>([])
const selectedUserId = ref('')
const selectedRole = ref('')
const isLoading = ref(false)
const isSubmitting = ref(false)

const canManage = computed(() => canInWorkspace('workspace:member:write', props.workspaceId))

/** Localized labels for the built-in workspace roles. */
const builtinRoleLabels: Record<string, string> = {
  workspace_owner: 'workspace.roleOwner',
  engineer: 'workspace.roleEngineer',
  analyst: 'workspace.roleAnalyst',
  viewer: 'workspace.roleViewer',
}

// Tenant members that are not yet part of this workspace.
const availableCandidates = computed(() => {
  const existing = new Set(members.value.map((m) => m.userId))
  return candidates.value.filter((c) => !existing.has(c.userId))
})

function usernameOf(member: WorkspaceMember): string {
  return member.username || candidates.value.find((c) => c.userId === member.userId)?.username || member.userId
}

/**
 * Display name of a role code: built-in roles are localized, custom ones use
 * the name the tenant gave them.
 */
function roleLabel(code: string): string {
  const key = builtinRoleLabels[code]
  if (key) return t(key)
  return roles.value.find((role) => role.code === code)?.name || code
}

async function load() {
  isLoading.value = true
  try {
    const [memberList, candidateList, roleList] = await Promise.all([
      workspaceStore.listMembers(props.workspaceId),
      workspaceStore.fetchTenantMemberCandidates(),
      rbacStore.listRoles('workspace'),
    ])
    members.value = memberList
    candidates.value = candidateList
    roles.value = roleList
    selectedUserId.value = availableCandidates.value[0]?.userId || ''
    selectedRole.value = roleList[0]?.code || ''
  } catch (e) {
    notifyError(e, 'Failed to load workspace members')
  } finally {
    isLoading.value = false
  }
}

async function addMember() {
  if (!selectedUserId.value || !selectedRole.value || isSubmitting.value) return
  isSubmitting.value = true
  try {
    await workspaceStore.addMember(props.workspaceId, selectedUserId.value, selectedRole.value)
    await load()
  } catch (e) {
    notifyError(e, 'Failed to add workspace member')
  } finally {
    isSubmitting.value = false
  }
}

async function removeMember(userId: string) {
  if (isSubmitting.value) return
  isSubmitting.value = true
  try {
    await workspaceStore.removeMember(props.workspaceId, userId)
    await load()
  } catch (e) {
    notifyError(e, 'Failed to remove workspace member')
  } finally {
    isSubmitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="dialog-overlay" @click.self="emit('close')">
    <div class="dialog">
      <h3>{{ t('workspace.members') }} — {{ props.workspaceName }}</h3>

      <p v-if="isLoading" class="dialog-hint">{{ t('common.loading') }}</p>

      <template v-else>
        <!-- Current members -->
        <ul class="member-list">
          <li v-for="m in members" :key="m.userId" class="member-row">
            <span class="member-name">{{ usernameOf(m) }}</span>
            <span class="member-role">{{ roleLabel(m.role) }}</span>
            <button
              v-if="!m.isOwner && canManage"
              class="btn-remove"
              :disabled="isSubmitting"
              @click="removeMember(m.userId)"
            >
              {{ t('workspace.removeMember') }}
            </button>
            <span v-else-if="m.isOwner" class="member-owner-tag">
              {{ t('workspace.roleOwner') }}
            </span>
          </li>
        </ul>

        <!-- Add member -->
        <div v-if="canManage" class="add-member">
          <select v-model="selectedUserId" :disabled="availableCandidates.length === 0">
            <option v-if="availableCandidates.length === 0" value="">
              {{ t('workspace.candidateEmpty') }}
            </option>
            <option v-for="c in availableCandidates" :key="c.userId" :value="c.userId">
              {{ c.username }}
            </option>
          </select>
          <select v-model="selectedRole">
            <option v-for="role in roles" :key="role.code" :value="role.code">
              {{ roleLabel(role.code) }}
            </option>
          </select>
          <button
            class="btn-primary"
            :disabled="!selectedUserId || !selectedRole || isSubmitting"
            @click="addMember"
          >
            {{ t('workspace.addMember') }}
          </button>
        </div>
      </template>

      <div class="dialog-actions">
        <button class="btn-cancel" @click="emit('close')">{{ t('common.close') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.dialog {
  width: 520px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
}

.dialog h3 {
  font-size: 16px;
  margin-bottom: 16px;
  color: var(--text-primary);
}

.dialog-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
}

.member-list {
  list-style: none;
  margin: 0 0 16px;
  padding: 0;
  max-height: 240px;
  overflow-y: auto;
}

.member-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}

.member-name {
  flex: 1;
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.member-role {
  font-size: 11px;
  color: var(--text-muted);
}

.member-owner-tag {
  font-size: 11px;
  color: var(--accent);
}

.btn-remove {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 11px;
  padding: 4px 8px;
  cursor: pointer;
}

.btn-remove:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--text-primary);
}

.btn-remove:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.add-member {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.add-member select {
  flex: 1;
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-cancel {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
}

.btn-primary {
  background: var(--accent);
  border: none;
  border-radius: 6px;
  color: var(--accent-foreground);
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
