<script setup lang="ts">
/**
 * PlanActions - Bottom action bar with Run/Apply/Cancel/Reset/Go Back buttons.
 * Button visibility is driven by the plan store action state.
 * Layout: primary action on the left, Start Over + Go Back on the right.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import router from '@/router'
import { usePlanStore } from '@/stores/plan'

const { t } = useI18n()
const planStore = usePlanStore()

// Apply button label depends on whether a backfill is required.
const applyLabel = computed(() =>
  planStore.requiresBackfill ? t('plan.applyAndBackfill') : t('plan.applyChanges')
)

// Show a "Start Over" button once a plan has been run (any post-idle state).
const canStartOver = computed(() => !planStore.isIdle && !planStore.isProcessing)

function handleRun() {
  planStore.runPlan()
}

function handleApply() {
  planStore.applyPlan()
}

function handleCancel() {
  planStore.cancelPlan()
}

function handleReset() {
  planStore.reset()
}

function handleGoBack() {
  planStore.reset()
  router.push('/chat')
}
</script>

<template>
  <div class="plan-actions">
    <!-- Left: primary action -->
    <div class="actions-left">
      <!-- Run Plan (idle) -->
      <button v-if="planStore.isIdle" class="action-btn primary" @click="handleRun">
        <t-icon name="play" size="14px" />
        <span>{{ t('plan.runPlan') }}</span>
      </button>

      <!-- Running spinner -->
      <button v-if="planStore.isRunning" class="action-btn primary" disabled>
        <t-icon name="loading" size="14px" class="spinning" />
        <span>{{ t('plan.running') }}</span>
      </button>

      <!-- Apply (preview with changes) -->
      <button
        v-if="planStore.isPreview && planStore.hasChanges"
        class="action-btn success"
        @click="handleApply"
      >
        <t-icon name="send" size="14px" />
        <span>{{ applyLabel }}</span>
      </button>

      <!-- Applying spinner -->
      <button v-if="planStore.isApplying" class="action-btn success" disabled>
        <t-icon name="loading" size="14px" class="spinning" />
        <span>{{ t('plan.applying') }}</span>
      </button>

      <!-- Cancel (during processing) -->
      <button
        v-if="planStore.isRunning || planStore.isApplying"
        class="action-btn danger"
        @click="handleCancel"
      >
        <t-icon name="close-circle" size="14px" />
        <span>{{ t('common.cancel') }}</span>
      </button>
    </div>

    <!-- Right: Start Over + Go Back -->
    <div class="actions-right">
      <button v-if="canStartOver" class="action-btn secondary" @click="handleReset">
        <t-icon name="rollback" size="14px" />
        <span>{{ t('plan.startOver') }}</span>
      </button>
      <button class="action-btn secondary" @click="handleGoBack">
        <t-icon name="chevron-left" size="14px" />
        <span>{{ t('plan.goBack') }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.plan-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg-surface);
}

.actions-left,
.actions-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.action-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  transition: all 0.15s ease;
}

.action-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.action-btn.primary {
  background: var(--accent);
  color: var(--accent-foreground);
  box-shadow: var(--shadow-float);
}

.action-btn.primary:hover:not(:disabled) {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

.action-btn.success {
  background: var(--success);
  color: var(--accent-foreground);
  box-shadow: var(--shadow-float);
}

.action-btn.success:hover:not(:disabled) {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

.action-btn.danger {
  background: var(--error-subtle);
  color: var(--error);
  border: 1px solid var(--error);
}

.action-btn.danger:hover:not(:disabled) {
  background: var(--error);
  color: var(--accent-foreground);
}

.action-btn.secondary {
  background: var(--bg-hover);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}

.action-btn.secondary:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
