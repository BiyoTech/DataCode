<script setup lang="ts">
/**
 * AssetDagDeployPanel - Deployment panel for the Plan & Apply page's
 * asset-dag tab.
 *
 * Shows the current environment's Dagster deployment (its code location),
 * lands newly merged code with "Pull & Reload", and can diff two deployments'
 * asset / schedule definition sets — the orchestration half of an environment
 * diff.
 *
 * This panel owns STEP 2 of promoting code between environments: making the
 * merged code live. Step 1 (the Git merge) lives in the Git panel, and the data
 * half (plan & apply) in this page's SQL Warehouse tab. The three steps are
 * deliberately separate user actions rather than one atomic "promote": a merge
 * conflict is an intermediate state only a human with a working copy can leave,
 * so an atomic endpoint would always degrade into "try, report conflict, wait
 * for a human, retry" — the same sequence, only harder to audit and to retry
 * step by step.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import { useWorkspaceStore } from '@/stores/workspace'
import { useDagsterStore } from '@/stores/dagster'
import { useEnvironmentStore } from '@/stores/environment'
import { useGitStore } from '@/stores/git'
import { notifyError } from '@/utils/notifyError'
import type { DeploymentDiff, DeploymentInfo } from '@/types/dagster'

const { t } = useI18n()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const environmentStore = useEnvironmentStore()
const dagster = useDagsterStore()
const gitStore = useGitStore()

const workspacePath = computed(() => workspaceStore.currentWorkspace?.path || '')
const deployment = computed(() => dagster.deployment)

// Deployments of the CURRENT workspace only (one per environment); drives the
// diff selectors and the inventory list. The raw service listing spans every
// workspace it has loaded, so refresh() filters by workspace_id.
const deployments = ref<DeploymentInfo[]>([])
const listLoading = ref(false)
const reloading = ref(false)

// Diff selectors: target defaults to the current environment's deployment,
// base to the workspace's production deployment.
const diffTarget = ref('')
const diffBase = ref('')
const diffResult = ref<DeploymentDiff | null>(null)
const diffLoading = ref(false)

const productionDeployment = computed(
  () => deployments.value.find((d) => d.env_type === 'production') || null,
)

/** True when a completed diff found no definition differences at all. */
const diffEmpty = computed(
  () =>
    diffResult.value !== null &&
    !diffResult.value.assets.added.length &&
    !diffResult.value.assets.changed.length &&
    !diffResult.value.assets.removed.length &&
    !diffResult.value.schedule_changes.length,
)

/** Ensure the current deployment exists and refresh the deployment inventory. */
async function refresh(): Promise<void> {
  const path = workspacePath.value
  if (!path) return
  listLoading.value = true
  try {
    await dagster.ensureDeployment(path)
    // The service lists every deployment it serves (across workspaces), but a
    // definition diff is only meaningful between environments of ONE workspace
    // — same Git repo, different branches. Match by workspace_id, NOT by
    // workspace_path: a working-copy path embeds {tenant}/{workspace}/{user}/
    // envs/{environment}, so a path match would keep only the current user's
    // current environment and drop every other environment of the workspace.
    const workspaceId = workspaceStore.currentWorkspace?.id || ''
    deployments.value = (await dagster.fetchDeployments()).filter(
      (d) => d.workspace_id === workspaceId,
    )
    // Pre-select sensible diff defaults, and re-anchor selections that point
    // at deployments no longer served (e.g. after a workspace switch).
    const known = new Set(deployments.value.map((d) => d.deployment_name))
    if (!known.has(diffTarget.value)) {
      diffTarget.value = deployment.value?.deployment_name || ''
    }
    if (!known.has(diffBase.value)) {
      diffBase.value = productionDeployment.value?.deployment_name || ''
    }
    // A previous diff compared the old inventory; drop it to avoid showing
    // stale results against a different environment.
    diffResult.value = null
    // Refresh ahead/behind so the panel can tell the user that merged code is
    // waiting on the remote and has not been pulled into this copy yet.
    await gitStore.fetchStatus()
  } catch (e) {
    notifyError(e, 'Failed to load deployments')
  } finally {
    listLoading.value = false
  }
}

/**
 * Bring the remote code into this working copy and re-import it.
 *
 * The two halves are one user action on purpose: reloading without pulling
 * re-imports the SAME files and looks like a no-op, which is exactly the
 * confusion a freshly merged branch produces. Nothing watches the filesystem —
 * code becomes live through this audited action, never because a file changed.
 */
async function pullAndReload(): Promise<void> {
  const path = workspacePath.value
  if (!path || reloading.value) return
  reloading.value = true
  try {
    // A failed pull must abort: reloading stale code would report success while
    // the merged commits are still absent.
    const result = await gitStore.pull()
    if (!result) {
      throw new Error(gitStore.error || t('deploy.pullReloadFailed'))
    }
    // A conflicted pull must abort too: the working tree now holds conflict
    // markers, so importing it would load code that does not even parse.
    if (result.conflicted) {
      MessagePlugin.warning(
        t('deploy.pullReloadConflicted', { count: result.conflicts.length })
      )
      return
    }
    await dagster.reloadDeployment(path)
    MessagePlugin.success(t('deploy.pullReloadSuccess'))
  } catch (e) {
    notifyError(e, 'Failed to pull and reload code location')
  } finally {
    reloading.value = false
  }
}

/** Reload the current environment's code location (after local asset-dag edits). */
async function reload(): Promise<void> {
  const path = workspacePath.value
  if (!path || reloading.value) return
  reloading.value = true
  try {
    await dagster.reloadDeployment(path)
    MessagePlugin.success(t('deploy.reloadSuccess'))
  } catch (e) {
    notifyError(e, 'Failed to reload code location')
  } finally {
    reloading.value = false
  }
}

/** Compare the selected target deployment against the selected base. */
async function runDiff(): Promise<void> {
  if (!diffTarget.value || !diffBase.value || diffLoading.value) return
  diffLoading.value = true
  diffResult.value = null
  try {
    diffResult.value = await dagster.diffDeployments(diffTarget.value, diffBase.value)
  } catch (e) {
    notifyError(e, 'Failed to compare deployments')
  } finally {
    diffLoading.value = false
  }
}

/** Display label for a deployment option: environment plus its unique name. */
function deploymentLabel(d: DeploymentInfo): string {
  return `${d.environment} — ${d.deployment_name}`
}

onMounted(refresh)

// Re-ensure on workspace / environment switch: the deployment belongs to one
// environment's working copy, so the panel must track the active one.
watch(
  () => [workspaceStore.currentWorkspace?.path, environmentStore.currentEnvironment],
  refresh,
)
</script>

<template>
  <div class="deploy-panel">
    <div class="deploy-scroll">
      <!-- No workspace selected -->
      <div v-if="!workspacePath" class="empty-hint">
        {{ t('deploy.noWorkspace') }}
      </div>

      <template v-else>
        <!-- Current deployment card -->
        <section class="deploy-section">
          <div class="section-head">
            <h3 class="section-title">{{ t('deploy.currentTitle') }}</h3>
            <button class="ghost-btn" :disabled="listLoading" @click="refresh">
              <t-icon name="refresh" size="13px" />
              {{ t('deploy.refresh') }}
            </button>
          </div>

          <div v-if="deployment" class="deploy-card">
            <div class="deploy-card-head">
              <span class="deploy-name" :title="deployment.deployment_name">
                {{ deployment.environment }}
              </span>
              <span class="badge" :class="deployment.alive ? 'badge-success' : 'badge-error'">
                {{ deployment.alive ? t('deploy.alive') : t('deploy.down') }}
              </span>
              <span class="badge" :class="`badge-${deployment.env_type}`">
                {{ deployment.env_type }}
              </span>
              <span
                class="badge"
                :class="deployment.schedules_enabled ? 'badge-success' : 'badge-muted'"
              >
                {{
                  deployment.schedules_enabled
                    ? t('deploy.schedulesOn')
                    : t('deploy.schedulesOff')
                }}
              </span>
            </div>
            <div class="deploy-meta">
              <span class="meta-item" :title="deployment.deployment_name">
                {{ t('deploy.fieldLocation') }}: {{ deployment.location_name }}
              </span>
            </div>
            <div class="deploy-actions">
              <button class="primary-btn" :disabled="reloading" @click="pullAndReload">
                <t-icon name="download" size="13px" />
                {{ reloading ? t('deploy.reloading') : t('deploy.pullReload') }}
              </button>
              <button class="ghost-btn" :disabled="reloading" @click="reload">
                <t-icon name="refresh" size="13px" />
                {{ t('deploy.reload') }}
              </button>
              <button class="ghost-btn" @click="router.push('/asset-dag')">
                <t-icon name="chart-bubble" size="13px" />
                {{ t('deploy.openGraph') }}
              </button>
            </div>
          </div>
          <div v-else class="empty-hint">{{ t('deploy.empty') }}</div>
        </section>

        <!-- The remote holds commits this working copy does not: typically a
             merge that has landed on this environment's branch and still has to
             be pulled and re-imported to become live. -->
        <div v-if="gitStore.behind > 0" class="behind-hint">
          <t-icon name="info-circle" size="14px" />
          <span>{{ t('deploy.behindRemote', { count: gitStore.behind }) }}</span>
        </div>

        <!-- Cross-deployment definition diff -->
        <section class="deploy-section">
          <div class="section-head">
            <h3 class="section-title">{{ t('deploy.diffTitle') }}</h3>
          </div>
          <div class="diff-controls">
            <label class="diff-field">
              <span class="diff-label">{{ t('deploy.diffTarget') }}</span>
              <select v-model="diffTarget" class="diff-select" :disabled="diffLoading">
                <option v-for="d in deployments" :key="d.deployment_name" :value="d.deployment_name">
                  {{ deploymentLabel(d) }}
                </option>
              </select>
            </label>
            <label class="diff-field">
              <span class="diff-label">{{ t('deploy.diffBase') }}</span>
              <select v-model="diffBase" class="diff-select" :disabled="diffLoading">
                <option v-for="d in deployments" :key="d.deployment_name" :value="d.deployment_name">
                  {{ deploymentLabel(d) }}
                </option>
              </select>
            </label>
            <button
              class="primary-btn"
              :disabled="!diffTarget || !diffBase || diffTarget === diffBase || diffLoading"
              @click="runDiff"
            >
              {{ diffLoading ? t('deploy.diffRunning') : t('deploy.diffAction') }}
            </button>
          </div>

          <div v-if="diffResult" class="diff-result">
            <div v-if="diffEmpty" class="empty-hint">{{ t('deploy.diffNoChanges') }}</div>
            <template v-else>
              <div v-if="diffResult.assets.added.length" class="diff-group">
                <div class="diff-group-title added">
                  {{ t('deploy.diffAssetsAdded') }} ({{ diffResult.assets.added.length }})
                </div>
                <div v-for="key in diffResult.assets.added" :key="`a-${key}`" class="diff-item">
                  {{ key }}
                </div>
              </div>
              <div v-if="diffResult.assets.changed.length" class="diff-group">
                <div class="diff-group-title changed">
                  {{ t('deploy.diffAssetsChanged') }} ({{ diffResult.assets.changed.length }})
                </div>
                <div v-for="key in diffResult.assets.changed" :key="`c-${key}`" class="diff-item">
                  {{ key }}
                </div>
              </div>
              <div v-if="diffResult.assets.removed.length" class="diff-group">
                <div class="diff-group-title removed">
                  {{ t('deploy.diffAssetsRemoved') }} ({{ diffResult.assets.removed.length }})
                </div>
                <div v-for="key in diffResult.assets.removed" :key="`r-${key}`" class="diff-item">
                  {{ key }}
                </div>
              </div>
              <div v-if="diffResult.schedule_changes.length" class="diff-group">
                <div class="diff-group-title">
                  {{ t('deploy.diffSchedules') }} ({{ diffResult.schedule_changes.length }})
                </div>
                <div
                  v-for="sc in diffResult.schedule_changes"
                  :key="`s-${sc.name}`"
                  class="diff-item schedule"
                >
                  <span class="schedule-name">{{ sc.name }}</span>
                  <span class="badge" :class="`badge-change-${sc.change}`">
                    {{ t(`deploy.schedule_${sc.change}`) }}
                  </span>
                  <span v-if="sc.change === 'modified'" class="schedule-cron">
                    {{ sc.base_cron }} → {{ sc.cron }}
                  </span>
                  <span v-else-if="sc.cron" class="schedule-cron">{{ sc.cron }}</span>
                </div>
              </div>
            </template>
          </div>
        </section>

        <!-- Deployment inventory -->
        <section class="deploy-section">
          <div class="section-head">
            <h3 class="section-title">{{ t('deploy.allTitle') }}</h3>
          </div>
          <div v-if="!deployments.length" class="empty-hint">{{ t('deploy.empty') }}</div>
          <div v-else class="deploy-list">
            <div
              v-for="d in deployments"
              :key="d.deployment_name"
              class="deploy-row"
              :class="{ current: d.deployment_name === deployment?.deployment_name }"
            >
              <span class="row-env" :title="d.deployment_name">{{ d.environment }}</span>
              <span class="badge" :class="`badge-${d.env_type}`">{{ d.env_type }}</span>
              <span class="badge" :class="d.alive ? 'badge-success' : 'badge-error'">
                {{ d.alive ? t('deploy.alive') : t('deploy.down') }}
              </span>
              <span
                class="badge"
                :class="d.schedules_enabled ? 'badge-success' : 'badge-muted'"
              >
                {{ d.schedules_enabled ? t('deploy.schedulesOn') : t('deploy.schedulesOff') }}
              </span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.deploy-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-primary);
  color: var(--text-primary);
}

.deploy-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px 24px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}

.deploy-section {
  margin-bottom: 18px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  margin: 0;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.deploy-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}

.deploy-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.deploy-name {
  font-size: 13px;
  font-weight: 600;
  margin-right: 4px;
}

.deploy-meta {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-muted);
}

.meta-item {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.deploy-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.badge-success {
  background: var(--success-subtle);
  color: var(--success);
}

.badge-error {
  background: var(--error-subtle);
  color: var(--error);
}

.badge-muted {
  background: var(--bg-hover);
  color: var(--text-muted);
}

.badge-production {
  background: var(--error-subtle);
  color: var(--error);
}

.badge-shared {
  background: var(--info-subtle);
  color: var(--info);
}

.badge-sandbox {
  background: var(--bg-hover);
  color: var(--text-muted);
}

.badge-change-added {
  background: var(--success-subtle);
  color: var(--success);
}

.badge-change-removed {
  background: var(--error-subtle);
  color: var(--error);
}

.badge-change-modified {
  background: var(--info-subtle);
  color: var(--info);
}

.primary-btn,
.ghost-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.primary-btn {
  border: none;
  background: var(--accent);
  color: var(--accent-foreground, #fff);
}

.primary-btn:hover:not(:disabled) {
  filter: brightness(1.1);
}

.primary-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ghost-btn {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-primary);
}

.ghost-btn:hover:not(:disabled) {
  background: var(--bg-hover);
}

.ghost-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.behind-hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--info);
  background: var(--info-subtle);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 18px;
}

.behind-hint .t-icon {
  flex-shrink: 0;
  margin-top: 1px;
}

.diff-controls {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.diff-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 160px;
}

.diff-label {
  font-size: 11px;
  color: var(--text-muted);
}

.diff-select {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text-primary);
  outline: none;
}

.diff-select:focus {
  border-color: var(--accent);
}

.diff-result {
  margin-top: 12px;
}

.diff-group {
  margin-bottom: 12px;
}

.diff-group-title {
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 4px;
}

.diff-group-title.added {
  color: var(--success);
}

.diff-group-title.changed {
  color: var(--info);
}

.diff-group-title.removed {
  color: var(--error);
}

.diff-item {
  font-size: 12px;
  font-family: var(--font-mono, monospace);
  padding: 3px 8px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.diff-item.schedule {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: inherit;
}

.schedule-name {
  font-weight: 500;
}

.schedule-cron {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}

.deploy-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.deploy-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  background: var(--bg-surface);
  border: 1px solid transparent;
}

.deploy-row.current {
  border-color: var(--accent);
}

.row-env {
  flex: 1;
  font-size: 12px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-hint {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  padding: 20px 12px;
  line-height: 1.6;
}
</style>
