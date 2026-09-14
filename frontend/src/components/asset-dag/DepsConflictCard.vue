<script setup lang="ts">
/**
 * DepsConflictCard — a declaration the platform baseline cannot satisfy.
 *
 * Shown INSTEAD of the usual "reload to install" hint, because a reload cannot
 * fix this: the overlay is prepended to `PYTHONPATH`, so a version other than
 * the platform's would replace the platform's rather than coexist with it. The
 * only resolution is an edit, and every row here carries the exact edit.
 *
 * Why the fix is offered as a button: the backend already computed the
 * requirement string to use, so asking the user to retype it would be asking
 * them to reproduce a value the platform is holding. The file is rewritten
 * line-by-line, preserving their comments and ordering.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { MessagePlugin } from 'tdesign-vue-next'
import { useDepsStore } from '@/stores/deps'
import { notifyError } from '@/utils/notifyError'
import type { DepsConflict } from '@/types/dagster'
import DepsBaselineDialog from './DepsBaselineDialog.vue'
const props = defineProps<{
  conflicts: DepsConflict[]
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

const hasMany = computed(() => props.conflicts.length > 1)

async function apply(conflicts: DepsConflict[], token: string) {
  if (applying.value) return
  applying.value = token
  try {
    const changed = await depsStore.applySuggestions(props.workspacePath, conflicts)
    if (changed === 0) {
      // The file no longer declares what the conflict described — someone else
      // edited it. Reporting success would leave the user believing a fix
      // landed that did not.
      MessagePlugin.warning(t('deploy.depsConflict.fixFailed'))
    } else {
      MessagePlugin.success(t('deploy.depsConflict.fixed'))
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
  <div class="deps-conflict">
    <div class="conflict-head">
      <t-icon name="error-circle" size="15px" />
      <span class="conflict-title">{{ t('deploy.depsConflict.title') }}</span>
      <button class="link-btn" @click="baselineOpen = true">
        {{ t('deploy.depsConflict.viewBaseline') }}
      </button>
    </div>

    <p class="conflict-explain">{{ t('deploy.depsConflict.explain') }}</p>

    <table class="conflict-table">
      <thead>
        <tr>
          <th>{{ t('deploy.depsConflict.column') }}</th>
          <th>{{ t('deploy.depsConflict.platform') }}</th>
          <th>{{ t('deploy.depsConflict.suggested') }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in conflicts" :key="item.requirement">
          <td><code>{{ item.requirement }}</code></td>
          <td><code>{{ item.name }}=={{ item.platform_version }}</code></td>
          <td><code class="suggested">{{ item.suggested }}</code></td>
          <td class="row-action">
            <button
              class="ghost-btn"
              :disabled="!!applying"
              @click="apply([item], item.requirement)"
            >
              {{ t('deploy.depsConflict.fixOne') }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="hasMany" class="conflict-actions">
      <button class="primary-btn" :disabled="!!applying" @click="apply(conflicts, '*')">
        {{ t('deploy.depsConflict.fixAll') }}
      </button>
    </div>

    <DepsBaselineDialog v-model:visible="baselineOpen" />
  </div>
</template>

<style scoped>
.deps-conflict {
  border: 1px solid var(--td-error-color-3, #f5a3a3);
  background: var(--td-error-color-1, #fff1f0);
  border-radius: 6px;
  padding: 10px 12px;
  margin: 8px 0;
  font-size: 12px;
}

.conflict-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--td-error-color, #d54941);
}

.conflict-title {
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

.conflict-explain {
  margin: 6px 0 10px;
  color: var(--td-text-color-secondary, #666);
  line-height: 1.5;
}

.conflict-table {
  width: 100%;
  border-collapse: collapse;
}

.conflict-table th {
  text-align: left;
  font-weight: 500;
  color: var(--td-text-color-placeholder, #999);
  padding-bottom: 4px;
}

.conflict-table td {
  padding: 3px 8px 3px 0;
  vertical-align: middle;
}

.conflict-table code {
  font-family: var(--code-font, monospace);
}

.suggested {
  color: var(--td-success-color, #2ba471);
}

.row-action {
  text-align: right;
}

.conflict-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}
</style>
