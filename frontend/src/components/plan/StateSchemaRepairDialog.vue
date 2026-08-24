<script setup lang="ts">
/**
 * StateSchemaRepairDialog - explains a STATE_SCHEMA_MISMATCH 409 and offers
 * the one-click fix.
 *
 * Rendered once (by the Toolbar) and driven by the shared composable state:
 * both the Toolbar's change-count probe and the Plan panel report the same
 * mismatch into this single dialog, so the user never sees two competing
 * error surfaces for one problem.
 */
import { useI18n } from 'vue-i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import { useStateSchemaRepair } from '@/composables/useStateSchemaRepair'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const repairState = useStateSchemaRepair()

/** Confirm = rewrite config.yaml to the managed value, then close. */
async function handleRepair(): Promise<void> {
  const wsPath = workspaceStore.currentWorkspace?.path
  if (!wsPath) return
  const ok = await repairState.repair(wsPath)
  if (ok) {
    repairState.close()
  }
}

function handleCancel(): void {
  repairState.close()
}
</script>

<template>
  <t-dialog
    v-model:visible="repairState.visible.value"
    :header="t('plan.stateSchema.title')"
    :confirm-btn="{
      content: t('plan.stateSchema.repairAction'),
      loading: repairState.repairing.value,
    }"
    :cancel-btn="t('common.close')"
    width="480px"
    destroy-on-close
    @confirm="handleRepair"
    @cancel="handleCancel"
  >
    <div v-if="repairState.payload.value" class="mismatch-dialog">
      <p class="dialog-reason">
        {{ t('plan.stateSchema.reason', {
          gateway: repairState.payload.value.gateway ?? '',
          actual: repairState.payload.value.actual ?? '',
          expected: repairState.payload.value.expected ?? '',
        }) }}
      </p>
      <dl class="dialog-fields">
        <div class="field-row">
          <dt>{{ t('plan.stateSchema.fieldGateway') }}</dt>
          <dd class="mono">{{ repairState.payload.value.gateway }}</dd>
        </div>
        <div class="field-row">
          <dt>{{ t('plan.stateSchema.fieldActual') }}</dt>
          <dd class="mono bad">{{ repairState.payload.value.actual }}</dd>
        </div>
        <div class="field-row">
          <dt>{{ t('plan.stateSchema.fieldExpected') }}</dt>
          <dd class="mono good">{{ repairState.payload.value.expected }}</dd>
        </div>
      </dl>
      <p class="dialog-hint">{{ t('plan.stateSchema.hint') }}</p>
      <p class="dialog-hint secondary">
        {{ t('plan.stateSchema.manualHint', {
          expected: repairState.payload.value.expected ?? '',
        }) }}
      </p>
    </div>
  </t-dialog>
</template>

<style scoped>
.mismatch-dialog {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dialog-reason {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
}

.dialog-fields {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-secondary);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-size: 12px;
}

.field-row dt {
  flex-shrink: 0;
  width: 90px;
  color: var(--text-muted);
}

.field-row dd {
  margin: 0;
  word-break: break-all;
}

.mono {
  font-family: var(--font-mono, monospace);
}

.bad {
  color: var(--error);
}

.good {
  color: var(--success);
}

.dialog-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.dialog-hint.secondary {
  color: var(--text-muted);
}
</style>
