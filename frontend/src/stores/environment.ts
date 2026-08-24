/**
 * Environment store — the platform's development lines.
 *
 * An environment is ONE unit binding a Git branch, a SQLMesh virtual environment
 * and (from Phase 4) a Dagster deployment. It is a workspace-level shared asset:
 * every member sees the same list, which is why it is read from the platform
 * registry (`/workspaces/{id}/environments`) rather than from the SQLMesh engine.
 *
 * Switching environment is NOT a local selection: it materializes that
 * environment's Git worktree and makes it the active working copy. The current
 * environment is therefore DERIVED from the working-copy path
 * (`.../{user}/envs/{env}`), never stored separately — the backend rejects a
 * plan/apply whose target does not match the worktree it runs in, so a local
 * override could only ever produce errors. The last opened environment IS
 * remembered by the workspace store, but only as the restore hint for the next
 * page refresh (see `rememberedEnvironment`); this store never reads it.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { useWorkspaceStore } from '@/stores/workspace'

/** Lifecycle status of an environment, as recorded by the registry. */
export type EnvironmentStatus =
  | 'creating'
  | 'active'
  | 'applying'
  | 'promoting'
  | 'drifted'
  | 'archiving'
  | 'archived'
  | 'failed'

/** Environment kind. Production is unique per workspace and never expires. */
export type EnvironmentType = 'production' | 'shared' | 'sandbox'

export interface EnvironmentInfo {
  /** `{workspace_id}:{name}` — the registry's addressable identifier. */
  environmentId: string
  workspaceId: string
  name: string
  /** Git branch backing the environment (`main` for production, else `env/*`). */
  branch: string
  envType: EnvironmentType
  /** Environment this one was branched from; also the promote/diff target. */
  baseEnv: string
  ownerUserId: string
  status: EnvironmentStatus
  deploymentName: string
  schedulesEnabled: boolean
  isProduction: boolean
  /** Commit whose code produced the current data. Empty when never applied. */
  lastAppliedCommit: string
  /**
   * The last apply ran from a working copy with uncommitted changes, so the
   * data is NOT reproducible from `lastAppliedCommit` — it must be reported as
   * drift even when that commit matches the branch head.
   */
  lastAppliedDirty: boolean
  /** UTC ISO 8601, empty when unset. Rendered in the user's timezone. */
  lastAppliedAt: string
  ttlHours: number
  /** UTC ISO 8601, empty for environments that never expire. */
  expiresAt: string
  createdAt: string
  updatedAt: string
}

export interface CreateEnvironmentPayload {
  name: string
  envType?: EnvironmentType
  baseEnv?: string
  ttlHours?: number
}

/** Map a registry payload onto the store model. */
function toEnvironmentInfo(raw: any): EnvironmentInfo {
  return {
    environmentId: raw.environment_id,
    workspaceId: raw.workspace_id,
    name: raw.name,
    branch: raw.branch,
    envType: (raw.env_type || 'sandbox') as EnvironmentType,
    baseEnv: raw.base_env || 'prod',
    ownerUserId: raw.owner_user_id || '',
    status: (raw.status || 'active') as EnvironmentStatus,
    deploymentName: raw.deployment_name || '',
    schedulesEnabled: Boolean(raw.schedules_enabled),
    isProduction: Boolean(raw.is_production),
    lastAppliedCommit: raw.last_applied_commit || '',
    lastAppliedDirty: Boolean(raw.last_applied_dirty),
    lastAppliedAt: raw.last_applied_at || '',
    ttlHours: Number(raw.ttl_hours ?? 0),
    expiresAt: raw.expires_at || '',
    createdAt: raw.created_at || '',
    updatedAt: raw.updated_at || '',
  }
}

/**
 * Extract the environment a working copy is checked out to.
 * Layout: `{base}/{tenant}/{workspace}/{user}/envs/{environment}`.
 */
function environmentFromPath(workspacePath: string): string {
  if (!workspacePath) return ''
  const segments = workspacePath.replace(/\\/g, '/').replace(/\/+$/, '').split('/')
  // Positional check, NOT a search: an environment may legally be named "envs"
  // (`.../u1/envs/envs`), and lastIndexOf('envs') would hit that final segment
  // and silently fall back to production — the wrong apply target.
  if (segments.length < 2 || segments[segments.length - 2] !== 'envs') return ''
  return segments[segments.length - 1]
}

export const useEnvironmentStore = defineStore('environment', () => {
  const environments = ref<EnvironmentInfo[]>([])
  const isLoading = ref(false)
  const isSwitching = ref(false)
  /** Registry environment id currently being created or destroyed, if any. */
  const pendingEnvironmentId = ref('')
  /** Whether the list has been loaded at least once for the current workspace. */
  const isLoaded = ref(false)

  /**
   * Fallback used before a working copy exists (first render). Production is the
   * only environment guaranteed to exist in every workspace, and the registry
   * knows its actual name — a workspace may rename it.
   */
  const productionFallback = computed(
    () => environments.value.find((env) => env.isProduction)?.name || 'prod'
  )

  /**
   * Environment of the active working copy. Derived, so it can never disagree
   * with the code on disk.
   */
  const currentEnvironment = computed(() => {
    const workspaceStore = useWorkspaceStore()
    return (
      environmentFromPath(workspaceStore.currentWorkspace?.path || '') ||
      productionFallback.value
    )
  })

  /** Registry entry of the active environment, once the list is loaded. */
  const current = computed(
    () => environments.value.find((env) => env.name === currentEnvironment.value) || null
  )

  /** Selectable environments: archived ones are audit records, not targets. */
  const selectable = computed(() =>
    environments.value.filter((env) => env.status !== 'archived')
  )

  /**
   * Whether a working copy of this environment can be materialized.
   *
   * Mirrors the registry's own rule (`OPENABLE_STATUSES`): an environment that
   * is still being built, is being released or failed has no branch to check
   * out. Disabling it in the UI is what turns a backend 409 into an
   * explanation the user can act on.
   */
  function isOpenable(env: EnvironmentInfo): boolean {
    return ['active', 'applying', 'promoting', 'drifted'].includes(env.status)
  }

  /**
   * Code and data are out of sync, so the environment needs a new apply.
   *
   * Two independent causes, both of which break "the data view was produced by
   * this branch's code":
   *  - the last apply ran on uncommitted code, so no commit describes the data;
   *  - the branch has moved past the commit that produced the data.
   *
   * Commit hashes are compared on a normalized prefix because the two sides may
   * carry different lengths (the registry stores the full SHA, Git APIs often
   * return an abbreviation).
   */
  function hasCommitDrift(env: EnvironmentInfo, branchHeadCommit: string): boolean {
    if (env.lastAppliedDirty) return true
    if (!env.lastAppliedCommit || !branchHeadCommit) return false
    const length = Math.min(env.lastAppliedCommit.length, branchHeadCommit.length)
    return (
      env.lastAppliedCommit.slice(0, length).toLowerCase() !==
      branchHeadCommit.slice(0, length).toLowerCase()
    )
  }

  /** Load the workspace's environments from the registry. */
  async function fetchEnvironments(): Promise<void> {
    const workspaceStore = useWorkspaceStore()
    const workspaceId = workspaceStore.currentWorkspaceId
    if (!workspaceId) {
      environments.value = []
      return
    }

    isLoading.value = true
    try {
      const { data } = await api.get(`/workspaces/${workspaceId}/environments`)
      environments.value = (data?.environments || []).map(toEnvironmentInfo)
      isLoaded.value = true
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Create an environment: registry row, Git branch, worktree and SQLMesh
   * virtual environment. The caller is NOT switched to it automatically, so a
   * creation never discards the work in progress in the current environment.
   */
  async function createEnvironment(
    payload: CreateEnvironmentPayload
  ): Promise<EnvironmentInfo> {
    const workspaceStore = useWorkspaceStore()
    const workspaceId = workspaceStore.currentWorkspaceId
    if (!workspaceId) throw new Error('No workspace is open')

    const body: Record<string, unknown> = { name: payload.name }
    if (payload.envType) body.env_type = payload.envType
    if (payload.baseEnv) body.base_env = payload.baseEnv
    if (payload.ttlHours !== undefined) body.ttl_hours = payload.ttlHours

    const { data } = await api.post(`/workspaces/${workspaceId}/environments`, body)
    const created = toEnvironmentInfo(data)
    environments.value = [
      ...environments.value.filter((env) => env.environmentId !== created.environmentId),
      created,
    ]
    return created
  }

  /**
   * Switch the active working copy to another environment.
   *
   * This materializes the environment's worktree if the caller does not hold it
   * yet; uncommitted work in the previous environment stays untouched because
   * each environment is a separate directory.
   */
  async function switchEnvironment(environmentId: string): Promise<string> {
    const workspaceStore = useWorkspaceStore()

    isSwitching.value = true
    try {
      const { data } = await api.post(
        `/environments/${encodeURIComponent(environmentId)}/open`
      )
      // The environment name is forwarded so the workspace store can remember
      // it as the restore hint for the next page refresh.
      workspaceStore.applyOpenedEnvironment(
        data.workspace_path,
        data.branch,
        data.environment
      )
      return data.environment as string
    } finally {
      isSwitching.value = false
    }
  }

  /**
   * Reconcile an environment against Git and the SQLMesh state, repairing what
   * can be repaired without a data cost (a missing branch, a missing virtual
   * environment, an archived environment's leftovers).
   *
   * This is how a `drifted` environment recovers: a drift caused by an
   * unreachable warehouse at creation time is invisible to the user and cannot
   * be fixed by any other action. Returns the report so the caller can tell
   * "repaired" from "still broken".
   */
  async function reconcileEnvironment(
    environmentId: string
  ): Promise<{ driftCount: number; repairedCount: number }> {
    pendingEnvironmentId.value = environmentId
    try {
      const { data } = await api.post(
        `/environments/${encodeURIComponent(environmentId)}/reconcile`
      )
      // The status may have changed (drifted -> active), so re-read the registry
      // rather than guessing the new state locally.
      await fetchEnvironments()
      return {
        driftCount: Number(data?.drift_count ?? 0),
        repairedCount: Number(data?.repaired_count ?? 0),
      }
    } finally {
      pendingEnvironmentId.value = ''
    }
  }

  /**
   * Destroy an environment: releases its SQLMesh views, worktree and branch.
   * The registry row and its events are kept for audit.
   *
   * `force` discards uncommitted changes in the caller's working copy. Without
   * it the backend refuses (409) and names how many changes would be lost, so
   * the decision is always the user's.
   */
  async function destroyEnvironment(
    environmentId: string,
    force = false
  ): Promise<void> {
    pendingEnvironmentId.value = environmentId
    try {
      await api.delete(`/environments/${encodeURIComponent(environmentId)}`, {
        params: force ? { force: true } : undefined,
      })
      environments.value = environments.value.filter(
        (env) => env.environmentId !== environmentId
      )
    } finally {
      pendingEnvironmentId.value = ''
    }
  }

  /** Drop workspace-scoped state when the active workspace changes. */
  function reset(): void {
    environments.value = []
    isLoaded.value = false
    pendingEnvironmentId.value = ''
  }

  return {
    environments,
    isLoading,
    isSwitching,
    isLoaded,
    pendingEnvironmentId,
    currentEnvironment,
    current,
    selectable,
    isOpenable,
    hasCommitDrift,
    fetchEnvironments,
    createEnvironment,
    switchEnvironment,
    reconcileEnvironment,
    destroyEnvironment,
    reset,
  }
})
