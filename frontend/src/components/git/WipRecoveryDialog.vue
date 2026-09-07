<script setup lang="ts">
/**
 * WipRecoveryDialog - offers the user's snapshot decision (design 07 Q6).
 *
 * Rendered once (by AppShell) and driven by the WIP store: recovering or
 * discarding someone's uncommitted work is strictly the OWNER's decision, so
 * the dialog presents both actions explicitly and never auto-applies. The
 * close button means "decide later" — the pending banner in the Git panel
 * keeps a non-blocking way back in.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWipStore } from '@/stores/wip'
import { formatLocalTime, formatRelativeTime } from '@/utils/timezone'

const { t } = useI18n()
const wipStore = useWipStore()

/** "3 minutes ago (2026-08-27 19:50)" — relative age plus local wall clock. */
const takenAtLabel = computed(() => {
  const taken = wipStore.pendingSnapshot?.taken_at
  if (!taken) return ''
  return `${formatRelativeTime(taken)} · ${formatLocalTime(taken, 'YYYY-MM-DD HH:mm')}`
})

const takenAtHint = computed(() =>
  wipStore.pendingSnapshot?.taken_at
    ? t('git.wip.pendingBody', { time: takenAtLabel.value })
    : t('git.wip.pendingBodyUnknownAge'),
)

async function handleRecover(): Promise<void> {
  await wipStore.recover()
}

async function handleDiscard(): Promise<void> {
  await wipStore.discard()
}

function handleLater(): void {
  wipStore.dialogVisible = false
}
</script>

<template>
  <t-dialog
    v-model:visible="wipStore.dialogVisible"
    :header="t('git.wip.pendingTitle')"
    :footer="false"
    :close-on-overlay-click="false"
    width="500px"
  >
    <div class="wip-dialog">
      <p class="dialog-text">{{ takenAtHint }}</p>
      <p class="dialog-hint">{{ t('git.wip.pendingPausedHint') }}</p>
      <p class="dialog-hint secondary">{{ t('git.wip.pendingDiscardHint') }}</p>

      <div class="dialog-actions">
        <t-button
          variant="outline"
          theme="danger"
          :loading="wipStore.isDiscarding"
          @click="handleDiscard"
        >
          {{ t('git.wip.discard') }}
        </t-button>
        <t-button
          theme="primary"
          :loading="wipStore.isRecovering"
          @click="handleRecover"
        >
          {{ t('git.wip.recover') }}
        </t-button>
        <button class="later-link" type="button" @click="handleLater">
          {{ t('git.wip.decideLater') }}
        </button>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.wip-dialog {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.dialog-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
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

.dialog-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}

.later-link {
  margin-left: auto;
  background: none;
  border: none;
  padding: 4px 6px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
}

.later-link:hover {
  color: var(--text-primary);
  text-decoration: underline;
}
</style>
