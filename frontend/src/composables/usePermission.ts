/**
 * Permission gating composable.
 *
 * Single entry point for UI authorization checks, so no component compares role
 * strings by hand. Two scopes are available:
 *
 * - `can(code)`         — tenant-scoped capability of the active identity;
 * - `canInWorkspace(c)` — capability inside the currently open workspace.
 *
 * Both are hints only: the gateway remains the sole authority and rejects a
 * forbidden request regardless of what the UI shows.
 */
import { onMounted } from 'vue'

import { useRbacStore } from '@/stores/rbac'
import { useWorkspaceStore } from '@/stores/workspace'

export function usePermission() {
  const rbacStore = useRbacStore()
  const workspaceStore = useWorkspaceStore()

  // Permissions of the active identity are needed by every gated view; loading
  // them here keeps call sites free of bootstrap logic.
  onMounted(() => {
    rbacStore.ensureIdentity().catch((error) => {
      console.error('[rbac] Failed to load identity permissions', error)
    })
  })

  /** Tenant-scoped capability check. */
  function can(permission: string): boolean {
    return rbacStore.can(permission)
  }

  /**
   * Workspace-scoped capability check against the currently open workspace.
   * A workspace the caller has no binding for grants nothing.
   */
  function canInWorkspace(permission: string, workspaceId?: string): boolean {
    const workspace = workspaceId
      ? workspaceStore.workspaces.find((ws) => ws.id === workspaceId)
      : workspaceStore.currentWorkspace
    return Boolean(workspace?.permissions.includes(permission))
  }

  return { can, canInWorkspace }
}
