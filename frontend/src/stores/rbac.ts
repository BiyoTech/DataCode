/**
 * RBAC store - reactive facade over the platform's authorization model.
 *
 * The gateway is the only authority: it resolves the caller's roles from the
 * database and publishes the effective permission codes. This store caches them
 * for UI gating and exposes the role management use cases (roles and their
 * permission composition are tenant-editable, including the built-in ones).
 *
 * The permission catalog itself is read-only: it is declared in backend code and
 * synchronized into the database on startup.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import api from '@/utils/api'
import { onSessionChange } from '@/utils/authSession'

/** Authorization scope a permission or role belongs to. */
export type RbacScope = 'tenant' | 'workspace'

/** One entry of the backend permission catalog. */
export interface PermissionDef {
  code: string
  scope: RbacScope
  resource: string
  action: string
  description: string
}

/** A role definition together with its permission composition. */
export interface RoleDefinition {
  id: string
  scope: RbacScope
  code: string
  name: string
  description: string
  isBuiltin: boolean
  permissions: string[]
  memberCount: number
}

/** Payload for creating or updating a role. */
export interface RoleInput {
  scope: RbacScope
  code: string
  name: string
  description: string
  permissions: string[]
}

export const useRbacStore = defineStore('rbac', () => {
  /** Effective tenant-scoped permissions of the active identity. */
  const tenantPermissions = ref<string[]>([])
  /** Display name of the active identity's tenant role. */
  const tenantRoleName = ref('')
  /** Read-only permission catalog, loaded on demand. */
  const catalog = ref<PermissionDef[]>([])
  const isIdentityLoaded = ref(false)
  const isCatalogLoaded = ref(false)

  // Switching identity (login, tenant switch, logout) invalidates everything.
  onSessionChange(() => {
    tenantPermissions.value = []
    tenantRoleName.value = ''
    isIdentityLoaded.value = false
    catalog.value = []
    isCatalogLoaded.value = false
  })

  /** Permission codes grouped by resource, for the role permission matrix. */
  const catalogByScope = computed<Record<RbacScope, PermissionDef[]>>(() => ({
    tenant: catalog.value.filter((p) => p.scope === 'tenant'),
    workspace: catalog.value.filter((p) => p.scope === 'workspace'),
  }))

  /** Whether the active identity holds a tenant-scoped permission. */
  function can(permission: string): boolean {
    return tenantPermissions.value.includes(permission)
  }

  /**
   * Load the effective permissions of the active identity.
   *
   * Idempotent: repeated calls are no-ops until the session changes, so any
   * component may call it on mount without extra round-trips.
   */
  async function ensureIdentity(force = false): Promise<void> {
    if (isIdentityLoaded.value && !force) return
    const { data } = await api.get<{ permissions?: string[]; roleName?: string }>('/me')
    tenantPermissions.value = data.permissions ?? []
    tenantRoleName.value = data.roleName ?? ''
    isIdentityLoaded.value = true
  }

  /** Load the permission catalog (requires tenant:role:read). */
  async function ensureCatalog(force = false): Promise<void> {
    if (isCatalogLoaded.value && !force) return
    const { data } = await api.get<{ permissions?: PermissionDef[] }>('/rbac/permissions')
    catalog.value = data.permissions ?? []
    isCatalogLoaded.value = true
  }

  /** List the tenant's roles for a scope, optionally only assignable ones. */
  async function listRoles(scope: RbacScope, assignableOnly = false): Promise<RoleDefinition[]> {
    const { data } = await api.get<{ roles?: RoleDefinition[] }>('/rbac/roles', {
      params: { scope, ...(assignableOnly ? { assignable: 1 } : {}) },
    })
    return data.roles ?? []
  }

  /** Create a custom role. */
  async function createRole(input: RoleInput): Promise<RoleDefinition> {
    const { data } = await api.post<RoleDefinition>('/rbac/roles', input)
    return data
  }

  /**
   * Rename a role and replace its permission set. Built-in roles are editable
   * this way; only their code is immutable.
   */
  async function updateRole(
    roleId: string,
    input: Pick<RoleInput, 'name' | 'description' | 'permissions'>
  ): Promise<RoleDefinition> {
    const { data } = await api.put<RoleDefinition>(`/rbac/roles/${roleId}`, input)
    return data
  }

  /** Delete a custom role that no member is bound to. */
  async function deleteRole(roleId: string): Promise<void> {
    await api.delete(`/rbac/roles/${roleId}`)
  }

  return {
    tenantPermissions,
    tenantRoleName,
    catalog,
    catalogByScope,
    can,
    ensureIdentity,
    ensureCatalog,
    listRoles,
    createRole,
    updateRole,
    deleteRole,
  }
})
