<script setup lang="ts">
/**
 * PlanPanel - Main Plan & Apply panel with Plan/History tabs.
 * Layout: Header → Tabs → Content (Plan or History) → Actions (fixed, plan-only).
 */
import { useI18n } from 'vue-i18n'
import router from '@/router'
import { usePlanStore } from '@/stores/plan'
import PlanOptions from './PlanOptions.vue'
import PlanStageTracker from './PlanStageTracker.vue'
import PlanChangePreview from './PlanChangePreview.vue'
import PlanTestResults from './PlanTestResults.vue'
import PlanLintResults from './PlanLintResults.vue'
import PlanActions from './PlanActions.vue'
import PlanHistoryList from './PlanHistoryList.vue'

const { t } = useI18n()
const planStore = usePlanStore()

function getStateBadgeClass(): string {
  switch (planStore.actionState) {
    case 'running': return 'badge-running'
    case 'preview': return 'badge-preview'
    case 'applying': return 'badge-applying'
    case 'done': return 'badge-done'
    case 'error': return 'badge-error'
    default: return 'badge-idle'
  }
}
</script>

<template>
  <div class="plan-panel">
    <!-- Header -->
    <div class="panel-header">
      <div class="header-left">
        <button class="back-btn" title="Back" @click="router.push('/app/chat')">
          <t-icon name="chevron-left" size="16px" />
        </button>
        <t-icon name="tools" size="16px" class="header-icon" />
        <h2 class="header-title">{{ t('plan.title') }}</h2>
      </div>
      <span class="state-badge" :class="getStateBadgeClass()">
        {{ t(`plan.state_${planStore.actionState}`) }}
      </span>
    </div>

    <!-- Tabs -->
    <div class="panel-tabs">
      <button
        class="tab-btn"
        :class="{ active: planStore.activeTab === 'plan' }"
        @click="planStore.switchTab('plan')"
      >
        {{ t('plan.tabPlan') }}
      </button>
      <button
        class="tab-btn"
        :class="{ active: planStore.activeTab === 'history' }"
        @click="planStore.switchTab('history')"
      >
        {{ t('plan.tabHistory') }}
      </button>
    </div>

    <!-- Plan Tab Content -->
    <div v-if="planStore.activeTab === 'plan'" class="panel-content">
      <PlanOptions />
      <PlanStageTracker />
      <PlanTestResults />
      <PlanLintResults />
      <PlanChangePreview />
      <div v-if="planStore.isIdle" class="idle-hint">
        <p class="idle-text">{{ t('plan.idleHint') }}</p>
      </div>
    </div>

    <!-- History Tab Content -->
    <div v-else class="panel-content">
      <PlanHistoryList />
    </div>

    <!-- Fixed bottom actions (plan tab only) -->
    <PlanActions v-if="planStore.activeTab === 'plan'" />
  </div>
</template>

<style scoped>
.plan-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-primary);
  color: var(--text-primary);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px 10px;
  background: var(--bg-surface);
}

.panel-tabs {
  display: flex;
  padding: 0 10px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
}

.tab-btn {
  padding: 8px 14px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.tab-btn:hover {
  color: var(--text-primary);
}

.tab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.back-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s ease;
}

.back-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.header-icon {
  color: var(--accent);
}

.header-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  letter-spacing: 0.2px;
}

.state-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.badge-idle {
  background: var(--bg-hover);
  color: var(--text-muted);
}

.badge-running {
  background: var(--accent-subtle);
  color: var(--accent);
  animation: pulse 1.5s ease-in-out infinite;
}

.badge-preview {
  background: var(--info-subtle);
  color: var(--info);
}

.badge-applying {
  background: var(--success-subtle);
  color: var(--success);
  animation: pulse 1.5s ease-in-out infinite;
}

.badge-done {
  background: var(--success-subtle);
  color: var(--success);
}

.badge-error {
  background: var(--error-subtle);
  color: var(--error);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.panel-content {
  flex: 1;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}

.idle-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}

.idle-text {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  line-height: 1.6;
}
</style>
