<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { useEnvironmentStore, type EnvironmentInfo } from '@/stores/environment'
import { useGatewayStore } from '@/stores/gateway'
import { useModelsStore } from '@/stores/models'
import api from '@/utils/api'
import { MessagePlugin } from 'tdesign-vue-next'
import { extractApiMessage, notifyError } from '@/utils/notifyError'
import {
  extractMismatchPayload,
  isStateSchemaMismatch,
  useStateSchemaRepair,
} from '@/composables/useStateSchemaRepair'
import IconButton from '@/components/common/IconButton.vue'
import StateSchemaRepairDialog from '@/components/plan/StateSchemaRepairDialog.vue'

interface PlanChanges {
  added?: Array<{ name: string }>
  removed?: Array<{ name: string }>
  modified?: {
    direct?: Array<{ name: string }>
    indirect?: Array<{ name: string }>
    metadata?: Array<{ name: string }>
  }
}

const props = defineProps<{
  showSidebar: boolean
  showChat: boolean
  showChatToggle?: boolean
}>()

defineEmits<{
  (e: 'toggle-sidebar'): void
  (e: 'toggle-chat'): void
}>()

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const environmentStore = useEnvironmentStore()
const gatewayStore = useGatewayStore()
const modelsStore = useModelsStore()

const showEnvDropdown = ref(false)
const showGatewayDropdown = ref(false)
const showChangesTooltip = ref(false)
const newEnvironmentName = ref('')

const isCreatingEnvironment = ref(false)
const changeCount = ref<number | null>(null)
const changedModels = ref<string[]>([])
const isLoadingChanges = ref(false)
const isRefreshingChanges = ref(false)

// The change-count probe hits the same 409 a real plan does. Its mismatch is
// reported once per workspace into the shared repair dialog; re-probes stay
// silent so the dialog cannot re-open on every file save.
const repairState = useStateSchemaRepair()
const mismatchPromptedWorkspaceId = ref('')

// Environment state is owned by the registry-backed store: it is a shared team
// asset, so it must not be mirrored into component-local refs.
const environments = computed(() => environmentStore.selectable)
const isLoadingEnvironments = computed(() => environmentStore.isLoading)
const environmentsLoaded = computed(() => environmentStore.isLoaded)
const currentEnvironment = computed(() => environmentStore.currentEnvironment)
const pendingEnvironmentId = computed(() => environmentStore.pendingEnvironmentId)
const isSwitchingEnvironment = computed(() => environmentStore.isSwitching)

/** Production first, then alphabetical: the production line is the reference. */
const sortedEnvironments = computed(() =>
  [...environments.value].sort((a, b) => {
    if (a.isProduction !== b.isProduction) return a.isProduction ? -1 : 1
    return a.name.localeCompare(b.name)
  })
)

/**
 * Production is the workspace's own line of development and is reached through
 * promotion, never destroyed. An environment already being released cannot be
 * destroyed twice either.
 */
function canDestroyEnvironment(env: EnvironmentInfo): boolean {
  return !env.isProduction && env.status !== 'archiving'
}

/** Short type label rendered next to the environment name. */
function environmentTypeLabel(env: EnvironmentInfo): string {
  return t(`environment.type.${env.envType}`)
}

/**
 * Repair is offered only where it can help: a drifted or failed environment is
 * missing a resource the reconciler can rebuild. Healthy environments have
 * nothing to repair, so the action stays out of the way.
 */
function canRepairEnvironment(env: EnvironmentInfo): boolean {
  return ['drifted', 'failed'].includes(env.status)
}

/**
 * Reconcile an environment: rebuild the branch or the virtual environment it is
 * missing. Safe to invoke repeatedly — it only performs repairs that cost no
 * data, and reports what it could not fix.
 */
async function repairEnvironment(env: EnvironmentInfo): Promise<void> {
  if (pendingEnvironmentId.value) return

  try {
    const { driftCount, repairedCount } = await environmentStore.reconcileEnvironment(
      env.environmentId
    )
    if (repairedCount > 0) {
      MessagePlugin.success(t('environment.repairSucceeded', { count: repairedCount }))
    } else if (driftCount > 0) {
      // Detected but not fixable here (an unreachable warehouse, a commit drift
      // that needs an apply): say so instead of implying success.
      MessagePlugin.warning(t('environment.repairIncomplete', { count: driftCount }))
    } else {
      MessagePlugin.success(t('environment.repairNothingToDo'))
    }
  } catch (e) {
    notifyError(e, t('environment.repairFailed'))
  }
}

// Gateway selector state (list is owned by the gateway store).
const gateways = computed(() => gatewayStore.gateways)
const currentGateway = computed(() => gatewayStore.currentGateway)
const isLoadingGateways = computed(() => gatewayStore.gatewaysLoading)
// Local connectivity hint for the selected gateway (never a global toast).
const gatewayConnectionOk = computed(() => gatewayStore.connectionOk)
const gatewayConnectionError = computed(() => gatewayStore.connectionError)
const isPingingGateway = computed(() => gatewayStore.pinging)

// True once environments have been loaded at least once for the current
// workspace. Environments load lazily (on opening the dropdown) so that
// opening a workspace never connects to the gateway DB.

const changesLabel = computed(() => {
  if (isLoadingChanges.value) return t('toolbar.loading')
  if (changeCount.value === null) return t('toolbar.na')
  return `${changeCount.value}`
})

const changedModelsTooltip = computed(() => {
  if (changedModels.value.length === 0) return t('toolbar.noChangedModels')
  return changedModels.value.join('\n')
})

/**
 * Load the workspace's environments from the platform registry.
 *
 * The registry — not the SQLMesh engine — is the source of truth: it is the same
 * list for every member and it carries the lifecycle metadata (type, status,
 * TTL, last applied commit) the engine does not know about.
 */
async function loadEnvironments(): Promise<void> {
  if (!workspaceStore.currentWorkspaceId) return

  try {
    await environmentStore.fetchEnvironments()
  } catch (e: any) {
    // Only a transport/infrastructure failure is a connectivity problem. An
    // answered request that was refused (403 without the env:read permission,
    // 404, a service error) must not be reported as "gateway unreachable" —
    // that would send the user debugging the wrong layer.
    const status = e?.response?.status
    if (status === undefined) {
      gatewayStore.connectionOk = false
      gatewayStore.connectionError = extractApiMessage(e, 'Gateway connection failed')
      return
    }
    notifyError(e, t('environment.loadFailed'))
  }
}

/**
 * Probe gateway connectivity before running a DB-dependent action. Returns
 * true when reachable. On failure the gateway store holds the error and the
 * Toolbar shows a local hint (no global toast), so callers simply skip the
 * action instead of triggering a 500 error dialog.
 */
async function ensureGatewayReachable(): Promise<boolean> {
  return await gatewayStore.pingGateway()
}

function extractChangedModels(changes: PlanChanges | undefined): string[] {
  if (!changes) return []
  const names = new Set<string>()

  for (const item of changes.added || []) {
    if (item?.name) names.add(item.name)
  }
  for (const item of changes.removed || []) {
    if (item?.name) names.add(item.name)
  }
  for (const item of changes.modified?.direct || []) {
    if (item?.name) names.add(item.name)
  }
  for (const item of changes.modified?.indirect || []) {
    if (item?.name) names.add(item.name)
  }
  for (const item of changes.modified?.metadata || []) {
    if (item?.name) names.add(item.name)
  }

  return [...names].sort((a, b) => a.localeCompare(b))
}

async function loadChangesGap(): Promise<void> {
  const workspacePath = workspaceStore.currentWorkspace?.path
  if (!workspacePath) {
    changeCount.value = null
    changedModels.value = []
    return
  }

  isLoadingChanges.value = true
  try {
    const { data } = await api.post('/sqlmesh/plan', {
      workspace_path: workspacePath,
      environment: currentEnvironment.value,
      gateway: currentGateway.value || undefined,
      plan_options: {
        skip_tests: true,
      },
    })

    const models = extractChangedModels(data?.changes)
    changedModels.value = models
    changeCount.value = models.length
  } catch (e) {
    changeCount.value = null
    changedModels.value = []
    // A mismatched state_schema blocks every plan/apply — surface it with the
    // repair dialog instead of failing silently. Other probe failures stay
    // quiet (ambient state, logged by the API interceptor) so a transient
    // error cannot toast on every refresh.
    const workspaceId = workspaceStore.currentWorkspace?.id ?? ''
    if (
      isStateSchemaMismatch(e) &&
      workspaceId &&
      mismatchPromptedWorkspaceId.value !== workspaceId
    ) {
      mismatchPromptedWorkspaceId.value = workspaceId
      repairState.open(extractMismatchPayload(e))
    }
  } finally {
    isLoadingChanges.value = false
  }
}

// After a successful repair the probe must refresh itself: the 409 it hit is
// gone, and the change count it feeds (the toolbar badge) is stale.
watch(
  () => repairState.lastRepairedAt.value,
  (repairedAt) => {
    if (repairedAt > 0) {
      mismatchPromptedWorkspaceId.value = ''
      void loadChangesGap()
    }
  },
)

function toggleEnvironmentDropdown(): void {
  const willOpen = !showEnvDropdown.value
  showEnvDropdown.value = willOpen
  // Lazily load environments on first open. This is a gateway-dependent
  // (DB) call, so probe connectivity first; on failure show the local hint
  // and skip loading rather than surfacing a global error.
  if (willOpen && !environmentsLoaded.value && !isLoadingEnvironments.value) {
    void (async () => {
      if (await ensureGatewayReachable()) {
        await loadEnvironments()
      }
    })()
  }
}

/**
 * Switch to another environment.
 *
 * This is not a local selection: it materializes that environment's Git
 * worktree and makes it the active working copy, so the code on screen always
 * matches the data being planned. Uncommitted work in the previous environment
 * is untouched — each environment is its own directory.
 */
async function selectEnvironment(env: EnvironmentInfo): Promise<void> {
  showEnvDropdown.value = false
  if (env.name === currentEnvironment.value) return
  // An environment with no materializable branch (creating / failed /
  // archiving) cannot be opened; say so instead of letting the backend 409.
  if (!environmentStore.isOpenable(env)) {
    notifyError(
      null,
      t('environment.notOpenable', {
        name: env.name,
        status: t(`environment.status.${env.status}`),
      })
    )
    return
  }

  try {
    await environmentStore.switchEnvironment(env.environmentId)
  } catch (e) {
    notifyError(e, t('environment.switchFailed'))
  }
}

function toggleGatewayDropdown(): void {
  showGatewayDropdown.value = !showGatewayDropdown.value
}

/**
 * Switch the global gateway and orchestrate all dependent reloads from a
 * single place: reset the current model selection (avoid leaking a model
 * from the previous gateway), clear lineage, reload the model list, and
 * recompute the changes count. Keeping the orchestration here prevents
 * implicit cross-store reload loops.
 */
async function selectGateway(gatewayName: string): Promise<void> {
  showGatewayDropdown.value = false
  if (gatewayName === gatewayStore.selectedGateway) return

  gatewayStore.setGateway(gatewayName)
  // Probe the newly selected gateway before any DB-dependent reload. On
  // failure, keep the local connection hint and skip the reloads so we never
  // trigger a global error dialog.
  if (!(await ensureGatewayReachable())) return

  // Reset model-scoped state so no stale selection/lineage from the previous
  // gateway remains visible.
  modelsStore.reset()
  await modelsStore.fetchModels()
  await loadChangesGap()
}

/**
 * Create an environment through the registry: it provisions the Git branch, the
 * worktree and the SQLMesh virtual environment as one unit. The caller is then
 * switched to it, since creating a development line means intending to work in
 * it.
 */
async function createEnvironment(): Promise<void> {
  const envName = newEnvironmentName.value.trim()
  if (!envName || isCreatingEnvironment.value) return

  isCreatingEnvironment.value = true
  try {
    const created = await environmentStore.createEnvironment({
      name: envName,
      baseEnv: currentEnvironment.value,
    })
    newEnvironmentName.value = ''
    showEnvDropdown.value = false
    await environmentStore.switchEnvironment(created.environmentId)
  } catch (e) {
    notifyError(e, t('environment.createFailed'))
  } finally {
    isCreatingEnvironment.value = false
  }
}

async function refreshChanges(): Promise<void> {
  if (isRefreshingChanges.value) return
  isRefreshingChanges.value = true
  try {
    // Changes computation needs the gateway DB; probe first and skip on
    // failure so the refresh button shows a local hint, not a global error.
    if (await ensureGatewayReachable()) {
      await loadChangesGap()
    }
  } finally {
    isRefreshingChanges.value = false
  }
}

/**
 * Destroy an environment: releases its SQLMesh views, worktree and branch while
 * keeping the audit trail. Irreversible, so it is confirmed first; the active
 * environment must be left before it can be destroyed, because destroying the
 * directory the user is working in would strand the UI on a dead path.
 */
async function destroyEnvironment(env: EnvironmentInfo): Promise<void> {
  if (!canDestroyEnvironment(env) || pendingEnvironmentId.value) return
  if (env.name === currentEnvironment.value) {
    notifyError(null, t('environment.cannotDestroyCurrent'))
    return
  }
  if (!window.confirm(t('environment.confirmDestroy', { name: env.name }))) return

  try {
    await environmentStore.destroyEnvironment(env.environmentId)
    return
  } catch (e: any) {
    // The backend refuses (409) while the working copy holds uncommitted work,
    // and reports how much would be lost. Discarding it must be an explicit
    // second decision, never an automatic retry.
    if (e?.response?.status !== 409) {
      notifyError(e, t('environment.destroyFailed'))
      return
    }
    const reason = extractApiMessage(e, t('environment.destroyFailed'))
    if (!window.confirm(t('environment.confirmDestroyForce', { reason }))) return
  }

  try {
    await environmentStore.destroyEnvironment(env.environmentId, true)
  } catch (e) {
    notifyError(e, t('environment.destroyFailed'))
  }
}

onMounted(async () => {
  // Only load the gateway LIST here (pure config.yaml read, no DB). Do NOT
  // auto-load environments or changes: those connect to the gateway DB and
  // must stay lazy so opening a workspace never fails on an unreachable /
  // unconfigured gateway.
  await gatewayStore.fetchGateways()
})

watch(
  () => workspaceStore.currentWorkspace?.path,
  async () => {
    showEnvDropdown.value = false
    showGatewayDropdown.value = false
    // The path changes both on workspace switch and on environment switch, and
    // both point at a different working copy: the gateway list comes from that
    // copy's config.yaml, so it is re-read. The environment list is
    // workspace-scoped and owned by the store, which the workspace lifecycle
    // resets — clearing it here would drop it on every environment switch.
    changeCount.value = null
    changedModels.value = []
    gatewayStore.reset()
    await gatewayStore.fetchGateways()
  }
)

watch(currentEnvironment, async () => {
  // Recompute changes only after environments were explicitly loaded and the
  // gateway is known reachable — never auto-connect on workspace open.
  if (!environmentsLoaded.value) return
  if (await ensureGatewayReachable()) {
    await loadChangesGap()
  }
})
</script>

<template>
  <header class="toolbar">
    <div class="toolbar-left">
      <IconButton
        class="toolbar-btn"
        :active="showSidebar"
        :label="t('layout.toggleSidebar')"
        @click="$emit('toggle-sidebar')"
      >
        <t-icon name="menu-fold" size="18px" />
      </IconButton>
      <span class="app-name">AgenticData</span>
    </div>

    <div class="toolbar-center">
      <slot name="center">
        <span class="breadcrumb">{{ t('workspaceInfo.title') }} / models</span>
      </slot>
    </div>

    <div class="toolbar-right">
      <div class="env-selector-wrap">
        <button class="env-selector-btn" @click="toggleGatewayDropdown">
          <span class="env-selector-label">{{ t('toolbar.gateway') }}:</span>
          <span class="env-selector-value">{{ currentGateway || t('toolbar.gatewayDefault') }}</span>
          <!-- Local connectivity indicator: only shown once probed. Never a
               global toast; hovering the dot reveals the error detail. -->
          <span
            v-if="isPingingGateway"
            class="gw-status gw-status--checking"
            :title="t('toolbar.gatewayChecking')"
          />
          <span
            v-else-if="gatewayConnectionOk === false"
            class="gw-status gw-status--error"
            :title="gatewayConnectionError || t('toolbar.gatewayConnectionError')"
          />
          <span
            v-else-if="gatewayConnectionOk === true"
            class="gw-status gw-status--ok"
            :title="t('toolbar.gatewayConnected')"
          />
          <t-icon name="chevron-down" size="12px" class="env-selector-arrow" />
        </button>

        <div v-if="showGatewayDropdown" class="env-dropdown-backdrop" @click="showGatewayDropdown = false" />

        <div v-if="showGatewayDropdown" class="env-dropdown" @click.stop>
          <div class="env-dropdown-header">{{ t('toolbar.gatewayList') }}</div>

          <div
            v-for="gw in gateways"
            :key="gw.name"
            class="env-item"
            :class="{ active: gw.name === currentGateway }"
          >
            <button class="env-item-main" @click="selectGateway(gw.name)">
              <t-icon v-if="gw.name === currentGateway" name="check" size="14px" class="env-item-check" />
              <span class="env-item-name">
                {{ gw.name }}
                <span class="env-item-type">({{ gw.dialect }})</span>
              </span>
            </button>
          </div>

          <div v-if="isLoadingGateways" class="env-dropdown-loading">{{ t('toolbar.loading') }}</div>

          <!-- Local gateway connection-error banner (replaces global toast). -->
          <div v-if="gatewayConnectionOk === false" class="gw-error-banner">
            <t-icon name="error-circle" size="14px" />
            <span class="gw-error-text">{{ gatewayConnectionError || t('toolbar.gatewayConnectionError') }}</span>
          </div>
        </div>
      </div>

      <div class="env-selector-wrap">
        <button class="env-selector-btn" :disabled="isSwitchingEnvironment" @click="toggleEnvironmentDropdown">
          <span class="env-selector-label">{{ t('toolbar.environment') }}:</span>
          <span class="env-selector-value">{{ currentEnvironment }}</span>
          <t-icon name="chevron-down" size="12px" class="env-selector-arrow" />
        </button>

        <div v-if="showEnvDropdown" class="env-dropdown-backdrop" @click="showEnvDropdown = false" />

        <div v-if="showEnvDropdown" class="env-dropdown" @click.stop>
          <div class="env-dropdown-header">{{ t('toolbar.environmentList') }}</div>

          <div
            v-for="env in sortedEnvironments"
            :key="env.environmentId"
            class="env-item"
            :class="{ active: env.name === currentEnvironment }"
          >
            <button
              class="env-item-main"
              :class="{ 'env-item-main--blocked': !environmentStore.isOpenable(env) }"
              :title="
                environmentStore.isOpenable(env)
                  ? undefined
                  : t('environment.notOpenable', {
                      name: env.name,
                      status: t(`environment.status.${env.status}`),
                    })
              "
              @click="selectEnvironment(env)"
            >
              <t-icon v-if="env.name === currentEnvironment" name="check" size="14px" class="env-item-check" />
              <span class="env-item-name" :class="env.envType">
                {{ env.name }}
                <span class="env-item-type">({{ environmentTypeLabel(env) }})</span>
                <span v-if="env.status !== 'active'" class="env-item-status">
                  {{ t(`environment.status.${env.status}`) }}
                </span>
                <!-- The data was produced by code that is not in Git, so no
                     commit describes it: a commit + re-apply clears this. -->
                <span
                  v-if="env.lastAppliedDirty"
                  class="env-item-status"
                  :title="t('environment.appliedDirtyHint')"
                >
                  {{ t('environment.appliedDirty') }}
                </span>
              </span>
            </button>
            <!-- Repair is the only way out of `drifted`: the missing resource
                 (branch or virtual environment) is invisible to the user. -->
            <button
              v-if="canRepairEnvironment(env)"
              class="env-item-repair"
              :disabled="pendingEnvironmentId === env.environmentId"
              :title="t('environment.repairHint')"
              @click="repairEnvironment(env)"
            >
              {{ t('environment.repair') }}
            </button>
            <button
              class="env-item-delete"
              :disabled="!canDestroyEnvironment(env) || pendingEnvironmentId === env.environmentId"
              :title="env.isProduction ? t('environment.productionProtected') : t('environment.destroy')"
              @click="destroyEnvironment(env)"
            >
              {{ t('common.delete') }}
            </button>
          </div>

          <div v-if="isLoadingEnvironments" class="env-dropdown-loading">{{ t('toolbar.loading') }}</div>
          <div v-else-if="environmentsLoaded && sortedEnvironments.length === 0" class="env-dropdown-loading">
            {{ t('environment.empty') }}
          </div>

          <div class="env-dropdown-create">
            <input
              v-model="newEnvironmentName"
              class="env-input"
              type="text"
              :placeholder="t('toolbar.createEnvironmentPlaceholder')"
              @keydown.enter="createEnvironment"
            />
            <button class="env-add-btn" :disabled="!newEnvironmentName.trim() || isCreatingEnvironment" @click="createEnvironment">
              {{ t('toolbar.add') }}
            </button>
          </div>
          <div class="env-dropdown-hint">{{ t('environment.nameHint') }}</div>
        </div>
      </div>

      <div class="gap-label-wrap">
        <IconButton
          class="changes-refresh-btn"
          :class="{ spinning: isRefreshingChanges }"
          :disabled="isRefreshingChanges"
          :label="t('layout.refreshChanges')"
          @click="refreshChanges"
        >
          <t-icon name="refresh" size="13px" class="refresh-icon" />
        </IconButton>
        <span
          class="gap-label"
          @mouseenter="showChangesTooltip = true"
          @mouseleave="showChangesTooltip = false"
        >
          {{ t('toolbar.changes') }}: <strong>{{ changesLabel }}</strong>
        </span>
        <div v-if="showChangesTooltip" class="changes-tooltip">
          <template v-if="changedModels.length > 0">
            <div v-for="model in changedModels" :key="model" class="changes-tooltip-item">{{ model }}</div>
          </template>
          <div v-else class="changes-tooltip-empty">{{ t('toolbar.noChangedModels') }}</div>
        </div>
      </div>

      <IconButton
        v-if="showChatToggle !== false"
        class="toolbar-btn"
        :active="showChat"
        :label="t('layout.toggleChat')"
        @click="$emit('toggle-chat')"
      >
        <t-icon name="chat" size="16px" />
      </IconButton>

      <slot name="right-extra" />
    </div>
  </header>

  <!-- Shared STATE_SCHEMA_MISMATCH repair surface: opened by this toolbar's
       change-count probe and by any other mismatch reporter. -->
  <StateSchemaRepairDialog />
</template>

<style scoped>
.toolbar {
  height: var(--toolbar-height);
  min-height: var(--toolbar-height);
  max-height: var(--toolbar-height);
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  flex-shrink: 0;
  overflow: visible;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  white-space: nowrap;
}

.toolbar-center {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  flex-shrink: 1;
  min-width: 0;
}

.app-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}

.breadcrumb {
  font-size: 12px;
  color: var(--text-muted);
}

.env-selector-wrap {
  position: relative;
}

.env-selector-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text-primary);
  cursor: pointer;
}

.env-selector-btn:hover {
  border-color: var(--accent-dim);
}

.env-selector-label {
  font-size: 12px;
  color: var(--text-muted);
}

.env-selector-value {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
}

.env-selector-arrow {
  color: var(--text-muted);
}

/* Gateway connectivity status dot (local hint, not a toast). */
.gw-status {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.gw-status--ok {
  background: var(--success, #21a675);
}

.gw-status--error {
  background: var(--error, #e34d59);
}

.gw-status--checking {
  background: var(--text-muted);
  animation: gw-pulse 1s ease-in-out infinite;
}

@keyframes gw-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.gw-error-banner {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  color: var(--error, #e34d59);
  font-size: 12px;
  line-height: 1.4;
}

.gw-error-text {
  word-break: break-word;
}

.env-dropdown-backdrop {
  position: fixed;
  inset: 0;
  z-index: 89;
}

.env-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 320px;
  max-height: 460px;
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float-lg);
  z-index: 90;
}

.env-dropdown-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
}

.env-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
}

.env-item:hover {
  background: var(--bg-hover);
}

.env-item-main {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--text-primary);
  cursor: pointer;
  padding: 4px 0;
}

.env-item-check {
  width: 16px;
  color: var(--accent);
}

.env-item-name {
  font-size: 13px;
  color: var(--text-primary);
}

.env-item-name.remote {
  color: var(--accent);
}

.env-item-name.local {
  color: var(--text-muted);
}

/* Sandbox environments are personal and expire, so they read as secondary. */
.env-item-name.sandbox {
  color: var(--text-muted);
}

.env-item-type {
  font-size: 12px;
  color: var(--text-muted);
  margin-left: 4px;
}

/* Recovery action, only rendered for environments that need it. */
.env-item-repair {
  flex-shrink: 0;
  padding: 2px 6px;
  border: none;
  border-radius: 3px;
  background: transparent;
  color: var(--warning-color, #e6a23c);
  font-size: 12px;
  cursor: pointer;
}

.env-item-repair:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* An environment with no materializable branch stays visible but inert. */
.env-item-main--blocked {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Any status other than "active" needs attention (creating, drifted, failed). */
.env-item-status {
  font-size: 11px;
  margin-left: 6px;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bg-hover, rgba(255, 255, 255, 0.08));
  color: var(--warning-color, #e6a23c);
}

.env-item-delete {
  border: none;
  border-radius: 6px;
  background: var(--error-subtle);
  color: var(--error);
  font-size: 11px;
  padding: 4px 8px;
  cursor: pointer;
}

.env-item-delete:disabled {
  background: transparent;
  color: var(--text-muted);
  cursor: not-allowed;
}

.env-dropdown-loading {
  padding: 8px 12px;
  color: var(--text-muted);
  font-size: 12px;
}

.env-dropdown-create {
  display: flex;
  gap: 8px;
  padding: 10px;
  border-top: 1px solid var(--border);
}

/* Naming rules are stricter than a Git branch, so they are stated up-front
   instead of surfacing as a validation error after submitting. */
.env-dropdown-hint {
  padding: 0 10px 10px;
  font-size: 11px;
  line-height: 1.4;
  color: var(--text-muted);
}

.env-input {
  flex: 1;
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}

.env-input:focus {
  border-color: var(--accent);
}

.env-add-btn {
  border: none;
  border-radius: 6px;
  background: var(--accent);
  color: var(--accent-foreground);
  font-size: 12px;
  font-weight: 600;
  padding: 0 12px;
  cursor: pointer;
}

.env-add-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.gap-label-wrap {
  position: relative;
}

.gap-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: default;
}

.gap-label strong {
  color: var(--accent);
}

.changes-tooltip {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  min-width: 200px;
  max-width: 360px;
  max-height: 320px;
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float-lg);
  z-index: 90;
  padding: 8px 0;
}

.changes-tooltip-item {
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.changes-tooltip-item:hover {
  background: var(--bg-hover);
}

.changes-tooltip-empty {
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-muted);
}

.changes-refresh-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  transition: color 0.2s ease;
}

.changes-refresh-btn:hover:not(:disabled) {
  color: var(--accent);
}

.changes-refresh-btn:disabled {
  cursor: not-allowed;
}

.changes-refresh-btn.spinning :deep(.refresh-icon) {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
