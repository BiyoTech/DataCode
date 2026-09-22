<script setup lang="ts">
/**
 * Reload progress — the stages a reload passes through and why it stopped.
 *
 * A reload installs the working copy's declared Python packages before it starts
 * the code location, so it can run for minutes. Showing only a spinner would make
 * the two failures users actually hit indistinguishable from a slow install:
 * a package that cannot be installed, and code that cannot be imported.
 *
 * Three things this renders that a toast cannot:
 *
 * - WHERE it is: the stage ladder comes from the job envelope, so the backend
 *   owns the order and this component cannot drift from it;
 * - WHAT it is doing: the installer's output, line by line;
 * - WHY it failed: the failure code decides the wording, and the transcript stays
 *   selectable so it can be pasted into an issue. When the platform archived the
 *   failure it also names the record under `logs/errors/`, which is what an agent
 *   reads to diagnose it.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ReloadJobInfo, ReloadStage } from '@/types/dagster'
import DepsConflictCard from './DepsConflictCard.vue'
import DepsRelaxedNotice from './DepsRelaxedNotice.vue'
import DepsTransitiveCard from './DepsTransitiveCard.vue'

const props = defineProps<{
  job: ReloadJobInfo | null
  output: string[]
  /**
   * Working copy, needed only to offer the conflict rewrite. Omit it and the
   * conflict is still explained — just without the one-click fix.
   */
  workspacePath?: string
}>()

const emit = defineEmits<{
  /** The declaration was rewritten; the parent re-reads the deployment. */
  (event: 'deps-fixed'): void
}>()

const { t } = useI18n()

// Failure output is collapsed by default: it is long, and the sentence above it
// is usually enough. Expanded automatically for a failure, because then it is
// the only actionable content on screen.
const showOutput = ref(false)

const stages = computed<ReloadStage[]>(() => props.job?.stages ?? [])
const currentIndex = computed(() => {
  if (!props.job) return -1
  const index = stages.value.indexOf(props.job.stage)
  // `failed` is not on the ladder: keep the last real stage highlighted so the
  // user can see WHERE it broke rather than just that it did.
  return index >= 0 ? index : stages.value.length - 1
})

const failed = computed(
  () => props.job?.state === 'failed' || props.job?.state === 'cancelled'
)
const running = computed(() => props.job?.state === 'running')

/** Status of one step: finished, in progress, failed here, or not started. */
function stepStatus(index: number): 'finish' | 'process' | 'error' | 'wait' {
  if (index < currentIndex.value) return 'finish'
  if (index > currentIndex.value) return 'wait'
  if (failed.value) return 'error'
  return props.job?.state === 'succeeded' ? 'finish' : 'process'
}

const failureText = computed(() => {
  const job = props.job
  if (!job || !failed.value) return ''
  const code = job.failure_code
  // Each code gets its own sentence because the user's next action differs:
  // fix the file's syntax, accept the platform's version, read pip's output,
  // fix the code, or wait for the platform.
  const known = [
    'deps_declaration_invalid',
    'deps_platform_conflict',
    'deps_platform_transitive',
    'deps_install_failed',
    'deps_no_wheel',
    'deps_index_not_configured',
    'deps_shadow_platform',
    'deps_too_large',
    'code_load_failed',
    'sandbox_unavailable',
  ]
  return known.includes(code)
    ? t(`deploy.reloadFailure.${code}`)
    : t('deploy.reloadFailure.internal_error')
})

/**
 * Conflicts to offer a rewrite for.
 *
 * Only with a workspace path: the fix edits a file in that copy, and a card
 * whose button cannot work would be worse than prose.
 */
const conflicts = computed(() =>
  props.workspacePath && failed.value ? (props.job?.conflicts ?? []) : [],
)

/**
 * Conflicts with a package the declaration never mentions.
 *
 * Only ever present after a failed reload: they are found by resolving, which
 * the read-only status deliberately never does.
 */
const transitiveConflicts = computed(() =>
  props.workspacePath && failed.value
    ? (props.job?.transitive_conflicts ?? [])
    : [],
)

/**
 * Platform packages the install had to override.
 *
 * Not gated on failure — the opposite: this is only ever set by an install that
 * SUCCEEDED, and it is published as soon as pip finished, so the notice appears
 * while the code location is still starting.
 */
const relaxed = computed(() => props.job?.relaxed ?? [])

const outputText = computed(() => props.output.join('\n'))
</script>

<template>
  <div v-if="job" class="rounded border border-gray-200 bg-gray-50 p-3 text-sm">
    <!-- Stage ladder. Order comes from the envelope, never from a local copy. -->
    <t-steps :current="currentIndex" size="small" :status="failed ? 'error' : 'process'">
      <t-step-item
        v-for="(stage, index) in stages"
        :key="stage"
        :title="t(`deploy.reloadStage.${stage}`)"
        :status="stepStatus(index)"
      />
    </t-steps>

    <div class="mt-3 flex items-center gap-2">
      <t-loading v-if="running" size="small" />
      <span :class="failed ? 'text-red-600' : 'text-gray-700'">
        {{ job.message || t(`deploy.reloadStage.${job.stage}`) }}
      </span>
      <t-tag v-if="job.attached" theme="warning" variant="light" size="small">
        {{ t('deploy.reloadAttached') }}
      </t-tag>
    </div>

    <p v-if="failureText" class="mt-2 text-red-600">{{ failureText }}</p>

    <!-- The rewrite per conflicting line. The reload already computed it, so
         retyping it would be asking the user to reproduce a value the platform
         is holding. -->
    <DepsConflictCard
      v-if="conflicts.length && workspacePath"
      :conflicts="conflicts"
      :workspace-path="workspacePath"
      @fixed="emit('deps-fixed')"
    />

    <!-- The blocking package is not in the file at all, so the card names it
         explicitly: "you declared feast, the platform's tenacity is what
         breaks". -->
    <DepsTransitiveCard
      v-if="transitiveConflicts.length && workspacePath"
      :conflicts="transitiveConflicts"
      :workspace-path="workspacePath"
      @fixed="emit('deps-fixed')"
    />

    <!-- The install worked, but only because the platform yielded a pin. Said
         out loud: this copy now imports a different version than the platform
         does, and nothing else on screen would ever mention it. -->
    <DepsRelaxedNotice v-if="relaxed.length" :relaxed="relaxed" />

    <p v-if="failed && job.error_log_id" class="mt-1 text-xs text-gray-500">
      <!-- Named so a user (or the agent they ask) can open the archived record. -->
      {{ t('deploy.reloadErrorLog', { id: job.error_log_id }) }}
    </p>

    <div v-if="outputText" class="mt-2">
      <t-link theme="primary" size="small" @click="showOutput = !showOutput">
        {{ showOutput ? t('deploy.hideOutput') : t('deploy.showOutput') }}
      </t-link>
      <!-- select-text, not a read-only input: the whole point is copying a pip
           error or a traceback out of it. -->
      <pre
        v-show="showOutput || failed"
        class="mt-2 max-h-64 select-text overflow-auto rounded bg-gray-900 p-2 font-mono text-xs text-gray-100"
        >{{ outputText }}</pre
      >
    </div>
  </div>
</template>
