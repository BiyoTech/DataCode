<script setup lang="ts">
/**
 * PlanStageTracker - Displays stage-based progress for Plan/Apply lifecycle.
 * Shows Tests → Changes → Backfills → Apply stages with status indicators.
 * A STATE_SCHEMA_MISMATCH error renders as a structured, repairable card
 * instead of the generic banner: the user sees exactly which value is wrong
 * and gets a one-click fix that re-runs the plan.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '@/stores/plan'
import { useWorkspaceStore } from '@/stores/workspace'
import { useStateSchemaRepair } from '@/composables/useStateSchemaRepair'
import type { StageStatus } from '@/types/plan'

const { t } = useI18n()
const planStore = usePlanStore()
const workspaceStore = useWorkspaceStore()
const repairState = useStateSchemaRepair()

interface DisplayStage {
  label: string
  status: StageStatus
}

const stages = computed<DisplayStage[]>(() => {
  // When idle, no stages to show
  if (planStore.isIdle) return []

  // Running plan preview
  if (planStore.isRunning) {
    return [
      { label: t('plan.stageAnalyzing'), status: 'running' },
    ]
  }

  // Preview ready - show test status + change/backfill summary
  if (planStore.isPreview) {
    const list: DisplayStage[] = []

    // Test status line
    if (planStore.testsSkipped) {
      list.push({ label: t('plan.testsSkipped'), status: 'success' })
    } else if (planStore.testsPassed) {
      list.push({ label: t('plan.testsPassed'), status: 'success' })
    }

    // Changes
    list.push({
      label: planStore.hasChanges ? t('plan.stageChanges') : t('plan.noChanges'),
      status: 'success',
    })

    // Backfills
    if (planStore.requiresBackfill) {
      list.push({ label: t('plan.stageBackfills'), status: 'success' })
    }
    return list
  }

  // Applying
  if (planStore.isApplying || planStore.isDone) {
    return planStore.applyStages.map(s => ({
      label: t(`plan.stage_${s.name}`),
      status: s.status,
    }))
  }

  return []
})

// The structured fields the backend inlines into the 409 body.
const mismatch = computed(() => {
  if (planStore.errorCode !== 'STATE_SCHEMA_MISMATCH') return null
  const payload = planStore.errorPayload ?? {}
  return {
    gateway: typeof payload.gateway === 'string' ? payload.gateway : '',
    expected: typeof payload.expected === 'string' ? payload.expected : '',
    actual: typeof payload.actual === 'string' ? payload.actual : '',
  }
})

/** Rewrite config.yaml to the managed value, then re-run the plan. */
async function handleRepairAndRetry(): Promise<void> {
  const wsPath = workspaceStore.currentWorkspace?.path
  if (!wsPath) return
  const ok = await repairState.repair(wsPath)
  if (ok) {
    await planStore.runPlan()
  }
}

function getStatusIcon(status: StageStatus): string {
  switch (status) {
    case 'success': return 'check'
    case 'fail': return 'close'
    case 'running': return 'loading'
    default: return 'circle'
  }
}

function getStatusClass(status: StageStatus): string {
  switch (status) {
    case 'success': return 'stage-success'
    case 'fail': return 'stage-fail'
    case 'running': return 'stage-running'
    default: return 'stage-pending'
  }
}
</script>

<template>
  <div
    v-if="stages.length > 0 || (planStore.isError && planStore.errorMessage && !planStore.testResults)"
    class="stage-tracker"
  >
    <div
      v-for="(stage, idx) in stages"
      :key="idx"
      class="stage-item"
      :class="getStatusClass(stage.status)"
    >
      <div class="stage-icon">
        <t-icon
          :name="getStatusIcon(stage.status)"
          size="14px"
          :class="{ spinning: stage.status === 'running' }"
        />
      </div>
      <span class="stage-label">{{ stage.label }}</span>
    </div>

    <!-- State-schema mismatch: structured, repairable error card -->
    <div v-if="mismatch" class="stage-error mismatch-card">
      <div class="mismatch-header">
        <t-icon name="error-circle" size="15px" />
        <span class="mismatch-title">{{ t('plan.stateSchema.title') }}</span>
      </div>
      <p class="mismatch-reason">
        {{ t('plan.stateSchema.reason', {
          gateway: mismatch.gateway,
          actual: mismatch.actual,
          expected: mismatch.expected,
        }) }}
      </p>
      <p class="mismatch-hint">{{ t('plan.stateSchema.hint') }}</p>
      <div class="mismatch-actions">
        <button
          class="repair-btn"
          :disabled="repairState.repairing.value"
          @click="handleRepairAndRetry"
        >
          <t-icon
            :name="repairState.repairing.value ? 'loading' : 'setting-1'"
            size="13px"
            :class="{ spinning: repairState.repairing.value }"
          />
          <span>{{ repairState.repairing.value
            ? t('plan.stateSchema.repairing')
            : t('plan.stateSchema.repairAction') }}</span>
        </button>
      </div>
      <p class="mismatch-hint secondary">
        {{ t('plan.stateSchema.manualHint', { expected: mismatch.expected }) }}
      </p>
    </div>

    <!-- Generic error message (test failures use the dedicated panel) -->
    <div
      v-else-if="planStore.isError && planStore.errorMessage && !planStore.testResults"
      class="stage-error"
    >
      <t-icon name="close" size="14px" />
      <span>{{ planStore.errorMessage }}</span>
    </div>

    <!-- Success message -->
    <div v-if="planStore.isDone" class="stage-done">
      <t-icon name="check" size="14px" />
      <span>{{ t('plan.applySuccess') }}</span>
    </div>
  </div>
</template>

<style scoped>
.stage-tracker {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.stage-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  transition: background 0.15s ease;
}

.stage-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  flex-shrink: 0;
}

.stage-label {
  color: var(--text-secondary);
}

.stage-success .stage-icon {
  color: var(--success);
}
.stage-success .stage-label {
  color: var(--success);
}

.stage-fail .stage-icon {
  color: var(--error);
}
.stage-fail .stage-label {
  color: var(--error);
}

.stage-running .stage-icon {
  color: var(--accent);
}
.stage-running .stage-label {
  color: var(--text-primary);
}

.stage-pending .stage-icon {
  color: var(--text-muted);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.stage-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: var(--error-subtle);
  border: 1px solid var(--error);
  border-radius: 6px;
  color: var(--error);
  font-size: 11px;
  margin-top: 4px;
}

/* Structured STATE_SCHEMA_MISMATCH card: reason + fields + repair action. */
.mismatch-card {
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  color: var(--text-primary);
}

.mismatch-header {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--error);
  font-weight: 600;
}

.mismatch-title {
  font-size: 12px;
}

.mismatch-reason {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-secondary);
  word-break: break-word;
}

.mismatch-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-muted);
}

.mismatch-hint.secondary {
  font-size: 10px;
}

.mismatch-actions {
  display: flex;
  gap: 8px;
}

.repair-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: none;
  border-radius: 6px;
  background: var(--accent);
  color: var(--accent-foreground);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.15s ease;
}

.repair-btn:hover:not(:disabled) {
  filter: brightness(1.1);
}

.repair-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.stage-done {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: var(--success-subtle);
  border: 1px solid var(--success);
  border-radius: 6px;
  color: var(--success);
  font-size: 11px;
  margin-top: 4px;
}
</style>
