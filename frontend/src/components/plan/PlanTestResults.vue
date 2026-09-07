<script setup lang="ts">
/**
 * PlanTestResults - Renders the "Plan Failed / One or More Tests Failed" panel.
 * Shows totals (total/succeeded/failed/errors/dialect) and per-test tracebacks,
 * matching the SQLMesh web UI test failure report.
 */
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
// TDesign icons used via <t-icon> in template
import { usePlanStore } from '@/stores/plan'
import type { TestResults } from '@/types/plan'

const { t } = useI18n()
const planStore = usePlanStore()

// vue-tsc type-checks the template on its own, so a `v-if` in it cannot narrow
// `planStore.testResults` (nullable) for the expressions it guards. Resolve the
// store value once into a complete object: the panel only renders when tests
// actually failed, so these zeros are never displayed.
const EMPTY_RESULTS: TestResults = {
  total: 0,
  successful: 0,
  failures: 0,
  errors: 0,
  dialect: '',
  details: [],
  traceback: '',
}

const results = computed<TestResults>(() => planStore.testResults ?? EMPTY_RESULTS)

// The backend always returns a `test_results` summary object even when every
// test passes (e.g. total=1, successful=1, failures=0). Treating a non-null
// `testResults` as a failure therefore wrongly shows "Plan Failed". Only show
// the panel when tests actually failed (or the summary reports failures/errors).
const shouldShow = computed(() => {
  if (planStore.testsFailed) return true
  return results.value.failures > 0 || results.value.errors > 0
})

// Failure details are expanded by default so users immediately see the cause.
const expanded = ref(true)

function toggle() {
  expanded.value = !expanded.value
}
</script>

<template>
  <div v-if="shouldShow" class="test-results">
    <!-- Plan Failed banner -->
    <div class="fail-banner">
      <t-icon name="error-circle" size="14px" />
      <span>{{ t('plan.planFailed') }}</span>
    </div>

    <!-- Tests failed collapsible -->
    <div class="fail-card">
      <button class="fail-card-header" @click="toggle">
        <t-icon name="minus-circle" size="14px" class="header-icon" />
        <span>{{ t('plan.testsFailedTitle') }}</span>
        <t-icon :name="expanded ? 'chevron-down' : 'chevron-right'" size="14px" class="chevron" />
      </button>

      <div v-if="expanded" class="fail-card-body">
        <!-- Totals -->
        <div class="totals">
          <span>{{ t('plan.testTotal') }}: {{ results.total }}</span>
          <span>{{ t('plan.testSucceeded') }}: {{ results.successful }}</span>
          <span>{{ t('plan.testFailed') }}: {{ results.failures }}</span>
          <span>{{ t('plan.testErrors') }}: {{ results.errors }}</span>
          <span>{{ t('plan.testDialect') }}: {{ results.dialect }}</span>
        </div>

        <!-- Per-test failure details -->
        <div
          v-for="(detail, idx) in results.details"
          :key="idx"
          class="failure-detail"
        >
          <div class="failure-message">— {{ detail.message }}</div>
          <pre class="failure-traceback">{{ detail.details }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.test-results {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.fail-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--error-subtle);
  border: 1px solid var(--error);
  border-radius: 8px;
  color: var(--error);
  font-size: 13px;
  font-weight: 500;
}

.fail-card {
  background: var(--error-subtle);
  border: 1px solid var(--error);
  border-radius: 8px;
  overflow: hidden;
}

.fail-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  background: var(--error-subtle);
  border: none;
  color: var(--error);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  text-align: left;
}

.header-icon {
  flex-shrink: 0;
}

.chevron {
  margin-left: auto;
}

.fail-card-body {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.totals {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--error);
  font-family: var(--font-mono, monospace);
}

.failure-detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.failure-message {
  font-size: 12px;
  color: var(--error);
  font-family: var(--font-mono, monospace);
}

.failure-traceback {
  margin: 0;
  padding: 12px 14px;
  background: var(--bg-primary);
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.6;
  font-family: var(--font-mono, monospace);
  color: var(--error);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
  max-height: 320px;
  overflow-y: auto;
}
</style>
