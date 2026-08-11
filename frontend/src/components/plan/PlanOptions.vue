<script setup lang="ts">
/**
 * PlanOptions - Plan configuration panel.
 * Includes Start/End date range (UTC) and collapsible Additional Options
 * (create_from, restate_models, and boolean plan flags).
 * Environment is controlled via the shared environment store (Toolbar picker).
 */
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
// TDesign icons used via <t-icon> in template
import { usePlanStore } from '@/stores/plan'

const { t } = useI18n()
const planStore = usePlanStore()

// Additional Options are collapsed by default (matches reference UI).
const isExpanded = ref(false)

function toggleExpand() {
  isExpanded.value = !isExpanded.value
}

// Boolean flag toggle helper.
type BoolKey = 'skip_tests' | 'skip_backfill' | 'forward_only' | 'auto_apply'
  | 'no_gaps' | 'include_unmodified' | 'no_auto_categorization'
function toggle(key: BoolKey) {
  planStore.setOption(key, !planStore.options[key])
}

// Restate models is edited as a comma-separated string.
const restateModelsText = computed({
  get: () => (planStore.options.restate_models ?? []).join(', '),
  set: (val: string) => {
    const list = val.split(',').map((s) => s.trim()).filter(Boolean)
    planStore.setOption('restate_models', list.length ? list : undefined)
  },
})

const createFrom = computed({
  get: () => planStore.options.create_from ?? '',
  set: (val: string) => planStore.setOption('create_from', val || undefined),
})

// Boolean flag definitions rendered in the two-column grid.
const flags: Array<{ key: BoolKey; label: string; desc: string }> = [
  { key: 'skip_tests', label: 'skipTests', desc: 'skipTestsDesc' },
  { key: 'include_unmodified', label: 'includeUnmodified', desc: 'includeUnmodifiedDesc' },
  { key: 'no_gaps', label: 'noGaps', desc: 'noGapsDesc' },
  { key: 'forward_only', label: 'forwardOnly', desc: 'forwardOnlyDesc' },
  { key: 'skip_backfill', label: 'skipBackfill', desc: 'skipBackfillDesc' },
  { key: 'auto_apply', label: 'autoApply', desc: 'autoApplyDesc' },
  { key: 'no_auto_categorization', label: 'noAutoCategorization', desc: 'noAutoCategorizationDesc' },
]
</script>

<template>
  <div class="plan-options">
    <!-- Date range -->
    <div class="date-row">
      <div class="date-field">
        <label class="date-label">{{ t('plan.startDate') }}</label>
        <input
          :value="planStore.planDates.start ?? ''"
          @input="planStore.setDate('start', ($event.target as HTMLInputElement).value)"
          class="date-input"
          placeholder="2023-12-13"
          :disabled="planStore.isProcessing"
        />
        <span class="date-hint">{{ t('plan.startDateHint') }}</span>
      </div>
      <div class="date-field">
        <label class="date-label">{{ t('plan.endDate') }}</label>
        <input
          :value="planStore.planDates.end ?? ''"
          @input="planStore.setDate('end', ($event.target as HTMLInputElement).value)"
          class="date-input"
          placeholder="2023-12-13"
          :disabled="planStore.isProcessing"
        />
        <span class="date-hint">{{ t('plan.endDateHint') }}</span>
      </div>
    </div>

    <!-- Additional Options collapsible header -->
    <button class="expand-toggle" @click="toggleExpand" :disabled="planStore.isProcessing">
      <span class="toggle-label">{{ t('plan.additionalOptions') }}</span>
      <t-icon :name="isExpanded ? 'minus' : 'add'" size="14px" />
    </button>

    <!-- Expanded options -->
    <div v-if="isExpanded" class="options-body">
      <!-- Create From + Restate Models -->
      <div class="text-row">
        <div class="text-field">
          <label class="field-label">{{ t('plan.createFrom') }}</label>
          <input
            v-model="createFrom"
            class="text-input"
            placeholder="dev"
            :disabled="planStore.isProcessing"
          />
          <span class="field-hint">{{ t('plan.createFromHint') }}</span>
        </div>
        <div class="text-field">
          <label class="field-label">{{ t('plan.restateModels') }}</label>
          <input
            v-model="restateModelsText"
            class="text-input"
            placeholder="project.model1, project.model2"
            :disabled="planStore.isProcessing"
          />
          <span class="field-hint">{{ t('plan.restateModelsHint') }}</span>
        </div>
      </div>

      <!-- Boolean flags grid -->
      <div class="flags-grid">
        <div v-for="flag in flags" :key="flag.key" class="flag-item">
          <div class="flag-text">
            <span class="flag-label">{{ t(`plan.${flag.label}`) }}</span>
            <span class="flag-desc">{{ t(`plan.${flag.desc}`) }}</span>
          </div>
          <button
            class="switch"
            :class="{ on: planStore.options[flag.key] }"
            :disabled="planStore.isProcessing"
            @click="toggle(flag.key)"
          >
            <span class="switch-knob" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plan-options {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}

/* Date range */
.date-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 14px;
}

.date-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.date-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.date-input,
.text-input {
  background: var(--input-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 10px;
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-mono, monospace);
  outline: none;
  transition: border-color 0.15s ease;
}

.date-input:focus,
.text-input:focus {
  border-color: var(--accent);
}

.date-input:disabled,
.text-input:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.date-hint,
.field-hint {
  font-size: 10px;
  color: var(--text-muted);
}

/* Expand toggle */
.expand-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  padding: 8px 12px;
  transition: color 0.15s ease;
}

.expand-toggle:hover:not(:disabled) {
  color: var(--text-primary);
}

.expand-toggle:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toggle-label {
  font-weight: 600;
}

.options-body {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.text-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.text-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

/* Boolean flags grid */
.flags-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 24px;
}

.flag-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.flag-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.flag-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
}

.flag-desc {
  font-size: 10px;
  color: var(--text-muted);
  line-height: 1.4;
}

/* Toggle switch */
.switch {
  flex-shrink: 0;
  width: 34px;
  height: 18px;
  border-radius: 9px;
  border: none;
  background: var(--border);
  position: relative;
  cursor: pointer;
  transition: background 0.15s ease;
  margin-top: 2px;
}

.switch.on {
  background: var(--accent);
}

.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.15s ease;
}

.switch.on .switch-knob {
  transform: translateX(16px);
}
</style>
