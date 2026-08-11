<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { useEnvironmentStore } from '@/stores/environment'
import { useGatewayStore } from '@/stores/gateway'
import { useModelsStore } from '@/stores/models'
import api from '@/utils/api'
import { extractApiMessage } from '@/utils/notifyError'
import IconButton from '@/components/common/IconButton.vue'

interface SqlmeshEnvironment {
  name: string
  snapshots: string[]
  type?: 'remote' | 'local'
}

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
const selectedEnvironment = ref(environmentStore.selectedEnvironment)
const environments = ref<SqlmeshEnvironment[]>([])
const pinnedEnvironments = ref<string[]>([])
const defaultTargetEnvironment = ref(environmentStore.defaultTargetEnvironment)

const isLoadingEnvironments = ref(false)
const isCreatingEnvironment = ref(false)
const deletingEnvironmentName = ref<string | null>(null)
const changeCount = ref<number | null>(null)
const changedModels = ref<string[]>([])
const isLoadingChanges = ref(false)
const isRefreshingChanges = ref(false)

const sortedEnvironments = computed(() => {
  return [...environments.value].sort((a, b) => {
    if (a.name === 'prod') return -1
    if (b.name === 'prod') return 1
    return a.name.localeCompare(b.name)
  })
})

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
const environmentsLoaded = ref(false)

const currentEnvironment = computed(() => selectedEnvironment.value || defaultTargetEnvironment.value || 'dev')

const changesLabel = computed(() => {
  if (isLoadingChanges.value) return t('toolbar.loading')
  if (changeCount.value === null) return t('toolbar.na')
  return `${changeCount.value}`
})

const changedModelsTooltip = computed(() => {
  if (changedModels.value.length === 0) return t('toolbar.noChangedModels')
  return changedModels.value.join('\n')
})

function canDeleteEnvironment(name: string): boolean {
  const isPinned = pinnedEnvironments.value.includes(name)
  return name !== 'prod' && !isPinned
}

async function loadEnvironments(): Promise<void> {
  const workspacePath = workspaceStore.currentWorkspace?.path
  if (!workspacePath) {
    environments.value = []
    return
  }

  isLoadingEnvironments.value = true
  try {
    const { data } = await api.get('/sqlmesh/environments', {
      params: { workspace_path: workspacePath },
    })

    const envMap = (data?.environments || {}) as Record<string, SqlmeshEnvironment>
    environments.value = Object.values(envMap)
    pinnedEnvironments.value = data?.pinned_environments || []
    defaultTargetEnvironment.value = data?.default_target_environment || 'dev'
    environmentStore.setDefault(defaultTargetEnvironment.value)

    const hasCurrent = environments.value.some((env) => env.name === selectedEnvironment.value)
    if (!hasCurrent) {
      selectedEnvironment.value = defaultTargetEnvironment.value || environments.value[0]?.name || 'dev'
    }
    // Sync to shared store
    environmentStore.setEnvironment(selectedEnvironment.value)
    environmentsLoaded.value = true
  } catch (e) {
    // Gateway DB may have become unreachable between the ping and this call.
    // Reflect it as a local connection hint instead of a global error dialog.
    gatewayStore.connectionOk = false
    gatewayStore.connectionError = extractApiMessage(e, 'Gateway connection failed')
    environments.value = []
  } finally {
    isLoadingEnvironments.value = false
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
  } catch {
    changeCount.value = null
    changedModels.value = []
  } finally {
    isLoadingChanges.value = false
  }
}

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

function selectEnvironment(environmentName: string): void {
  selectedEnvironment.value = environmentName
  environmentStore.setEnvironment(environmentName)
  showEnvDropdown.value = false
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

async function createEnvironment(): Promise<void> {
  const workspacePath = workspaceStore.currentWorkspace?.path
  const envName = newEnvironmentName.value.trim()
  if (!workspacePath || !envName || isCreatingEnvironment.value) return

  isCreatingEnvironment.value = true
  try {
    await api.post('/sqlmesh/apply', {
      workspace_path: workspacePath,
      environment: envName,
      plan_options: {
        create_from: currentEnvironment.value,
        include_unmodified: true,
        skip_backfill: true,
        skip_tests: true,
      },
    })

    newEnvironmentName.value = ''
    await loadEnvironments()
    selectedEnvironment.value = envName
    showEnvDropdown.value = false
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

async function deleteEnvironment(environmentName: string): Promise<void> {
  const workspacePath = workspaceStore.currentWorkspace?.path
  if (!workspacePath || !canDeleteEnvironment(environmentName)) return

  deletingEnvironmentName.value = environmentName
  try {
    await api.delete(`/sqlmesh/environments/${encodeURIComponent(environmentName)}`, {
      params: {
        workspace_path: workspacePath,
      },
    })

    await loadEnvironments()
  } finally {
    deletingEnvironmentName.value = null
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
    // Reset per-workspace state; reload only the gateway list (no DB).
    environmentsLoaded.value = false
    environments.value = []
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
        <button class="env-selector-btn" @click="toggleEnvironmentDropdown">
          <span class="env-selector-label">{{ t('toolbar.environment') }}:</span>
          <span class="env-selector-value">{{ currentEnvironment }}</span>
          <t-icon name="chevron-down" size="12px" class="env-selector-arrow" />
        </button>

        <div v-if="showEnvDropdown" class="env-dropdown-backdrop" @click="showEnvDropdown = false" />

        <div v-if="showEnvDropdown" class="env-dropdown" @click.stop>
          <div class="env-dropdown-header">{{ t('toolbar.environmentList') }}</div>

          <div
            v-for="env in sortedEnvironments"
            :key="env.name"
            class="env-item"
            :class="{ active: env.name === currentEnvironment }"
          >
            <button class="env-item-main" @click="selectEnvironment(env.name)">
              <t-icon v-if="env.name === currentEnvironment" name="check" size="14px" class="env-item-check" />
              <span class="env-item-name" :class="env.type">
                {{ env.name }}
                <span class="env-item-type">({{ env.type ?? 'remote' }})</span>
              </span>
            </button>
            <button
              class="env-item-delete"
              :disabled="!canDeleteEnvironment(env.name) || deletingEnvironmentName === env.name"
              @click="deleteEnvironment(env.name)"
            >
              {{ t('common.delete') }}
            </button>
          </div>

          <div v-if="isLoadingEnvironments" class="env-dropdown-loading">{{ t('toolbar.loading') }}</div>

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

.env-item-type {
  font-size: 12px;
  color: var(--text-muted);
  margin-left: 4px;
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
