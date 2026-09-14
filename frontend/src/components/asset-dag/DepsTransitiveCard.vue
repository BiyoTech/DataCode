<script setup lang="ts">
/**
 * DepsTransitiveCard — a declared package whose own dependencies the platform
 * baseline rejects.
 *
 * The sibling DepsConflictCard covers the case where the DECLARED package is
 * the problem. This one is the opposite: the declaration is compatible, and the
 * collision is with a package the file never mentions — `feast` needs
 * `tenacity<9` while the platform ships tenacity 9.1.4. No check of the file
 * can see it, so it can only be reported after a resolve, which is why it
 * reaches the UI from a failed reload and not from the read-only status.
 *
 * Reaching this card at all means the platform already tried releasing the pins
 * it was allowed to release and retried the install. So each row ends in one of
 * exactly two places, and the card says which: an edit the installer verified,
 * or a `protected` package that only an operator can move.
 *
 * What makes it worth a card rather than a paragraph: the backend resolved the
 * declaration with its upper bounds removed while diagnosing the failure, so
 * the rewrite it offers is one pip already verified. Asking the user to find a
 * working version themselves is asking them to redo a resolve they cannot run.
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { MessagePlugin } from 'tdesign-vue-next'
import { useDepsStore } from '@/stores/deps'
import { notifyError } from '@/utils/notifyError'
import type { DepsTransitiveConflict } from '@/types/dagster'
import DepsBaselineDialog from './DepsBaselineDialog.vue'

const props = defineProps<{
  conflicts: DepsTransitiveConflict[]
  workspacePath: string
}>()

const emit = defineEmits<{
  /** The declaration was rewritten; the caller refreshes its deps state. */
  (event: 'fixed'): void
}>()

const { t } = useI18n()
const depsStore = useDepsStore()

const applying = ref('')
const baselineOpen = ref(false)

async function apply(item: DepsTransitiveConflict, token: string) {
  if (applying.value || !item.suggested) return
  applying.value = token
  try {
    const changed = await depsStore.applySuggestions(props.workspacePath, [
      { name: item.name, suggested: item.suggested },
    ])
    // Zero means the line no longer declares this package — someone else
    // edited the file. Reporting success would claim a fix that did not land.
    if (changed === 0) {
      MessagePlugin.warning(t('deploy.depsTransitive.fixFailed'))
    } else {
      MessagePlugin.success(t('deploy.depsTransitive.fixed'))
    }
    emit('fixed')
  } catch (error) {
    notifyError(error)
  } finally {
    applying.value = ''
  }
}
</script>

<template>
  <div class="deps-transitive">
    <div class="transitive-head">
      <t-icon name="error-circle" size="15px" />
      <span class="transitive-title">{{ t('deploy.depsTransitive.title') }}</span>
      <button class="link-btn" @click="baselineOpen = true">
        {{ t('deploy.depsTransitive.viewBaseline') }}
      </button>
    </div>

    <p class="transitive-explain">{{ t('deploy.depsTransitive.explain') }}</p>

    <div v-for="item in conflicts" :key="item.requirement" class="transitive-row">
      <div class="transitive-declared">
        <code>{{ item.requirement }}</code>
      </div>

      <ul v-if="item.blockers.length" class="blocker-list">
        <li v-for="blocker in item.blockers" :key="blocker.name">
          {{ t(
            blocker.required_by
              ? 'deploy.depsTransitive.blockerVia'
              : 'deploy.depsTransitive.blocker',
            {
              name: blocker.name,
              required: blocker.required || '?',
              platform: blocker.platform_version,
              via: blocker.required_by,
            },
          ) }}
        </li>
      </ul>

      <div v-if="item.suggested" class="transitive-fix">
        <span class="fix-label">{{ t('deploy.depsTransitive.suggested') }}</span>
        <code class="suggested">{{ item.suggested }}</code>
        <span v-if="item.resolved_version" class="fix-version">
          {{ t('deploy.depsTransitive.resolvesTo', { version: item.resolved_version }) }}
        </span>
        <button
          class="ghost-btn"
          :disabled="!!applying"
          @click="apply(item, item.requirement)"
        >
          {{ t('deploy.depsTransitive.apply') }}
        </button>
      </div>
      <!-- The dead end, with the reason it is one. Naming the package matters:
           it is what the user quotes when asking an operator. -->
      <div v-else-if="item.protected?.length" class="transitive-dead-end">
        {{ t('deploy.depsTransitive.noFixProtected', {
          packages: item.protected.join(', '),
        }) }}
      </div>
      <div v-else class="transitive-dead-end">
        {{ t('deploy.depsTransitive.noFix') }}
      </div>
    </div>

    <DepsBaselineDialog v-model:visible="baselineOpen" />
  </div>
</template>

<style scoped>
.deps-transitive {
  border: 1px solid var(--td-error-color-3, #f5a3a3);
  background: var(--td-error-color-1, #fff1f0);
  border-radius: 6px;
  padding: 10px 12px;
  margin: 8px 0;
  font-size: 12px;
}

.transitive-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--td-error-color, #d54941);
}

.transitive-title {
  font-weight: 600;
}

.link-btn {
  margin-left: auto;
  border: none;
  background: none;
  color: var(--td-brand-color, #0052d9);
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}

.transitive-explain {
  margin: 6px 0 10px;
  color: var(--td-text-color-secondary, #666);
  line-height: 1.5;
}

.transitive-row + .transitive-row {
  margin-top: 10px;
  border-top: 1px dashed var(--td-error-color-3, #f5a3a3);
  padding-top: 8px;
}

.transitive-declared code {
  font-family: var(--code-font, monospace);
}

.blocker-list {
  margin: 4px 0 0;
  padding-left: 16px;
  color: var(--td-text-color-secondary, #666);
  line-height: 1.5;
}

.transitive-fix {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.fix-label {
  color: var(--td-text-color-placeholder, #999);
}

.transitive-fix code {
  font-family: var(--code-font, monospace);
}

.suggested {
  color: var(--td-success-color, #2ba471);
}

.fix-version {
  color: var(--td-text-color-placeholder, #999);
}

.transitive-dead-end {
  margin-top: 6px;
  color: var(--td-text-color-secondary, #666);
}
</style>
