<script setup lang="ts">
/**
 * DepsBaselineDialog — what the platform already provides.
 *
 * The list exists because the baseline used to be an implicit fact: whatever
 * the runtime image happened to contain, discoverable only by declaring a
 * version and being refused. A user cannot write a compatible declaration
 * against something they cannot read.
 *
 * The `declare as` column is the useful part — the exact string to paste into
 * `[project].dependencies`, so nobody has to derive the rule that a bound is
 * `>=` and never `==`.
 *
 * Curated by default: the full baseline is several hundred distributions, almost
 * all of them transitive, which is a wall nobody extracts an answer from.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDepsStore } from '@/stores/deps'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ (event: 'update:visible', value: boolean): void }>()

const { t } = useI18n()
const depsStore = useDepsStore()

const showAll = ref(false)
const filter = ref('')

// Fetched on first open rather than on mount: the dialog is a reference the
// user consults occasionally, and the store caches it for the session.
watch(
  () => props.visible,
  (open) => {
    if (open) depsStore.loadBaseline()
  },
)

const rows = computed(() => {
  const baseline = depsStore.baseline
  if (!baseline) return []
  const source = showAll.value ? baseline.packages : baseline.highlighted
  const needle = filter.value.trim().toLowerCase()
  // A filter implies the full set: searching within the curated view would hide
  // the very transitive package the user is asking about.
  const pool = needle ? baseline.packages : source
  return needle ? pool.filter((item) => item.name.includes(needle)) : pool
})

const total = computed(() => depsStore.baseline?.packages.length ?? 0)
</script>

<template>
  <t-dialog
    :visible="props.visible"
    :header="t('deploy.depsBaseline.title')"
    :footer="false"
    width="620px"
    @close="emit('update:visible', false)"
  >
    <div class="baseline-body">
      <p class="baseline-subtitle">
        {{
          t('deploy.depsBaseline.subtitle', {
            python: depsStore.baseline?.python_version || '?',
          })
        }}
      </p>

      <t-input
        v-model="filter"
        :placeholder="t('deploy.depsBaseline.name')"
        clearable
        class="baseline-filter"
      />

      <t-loading :loading="depsStore.loadingBaseline" size="small">
        <div v-if="!depsStore.baseline && !depsStore.loadingBaseline" class="baseline-empty">
          {{ t('deploy.depsBaseline.empty') }}
        </div>
        <table v-else class="baseline-table">
          <thead>
            <tr>
              <th>{{ t('deploy.depsBaseline.name') }}</th>
              <th>{{ t('deploy.depsBaseline.version') }}</th>
              <th>{{ t('deploy.depsBaseline.declare') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in rows" :key="item.name">
              <td>{{ item.name }}</td>
              <td>{{ item.version }}</td>
              <td><code>{{ item.suggested }}</code></td>
            </tr>
          </tbody>
        </table>
      </t-loading>

      <button v-if="!filter" class="link-btn" @click="showAll = !showAll">
        {{
          showAll
            ? t('deploy.depsBaseline.showCommon')
            : t('deploy.depsBaseline.showAll', { count: total })
        }}
      </button>
    </div>
  </t-dialog>
</template>

<style scoped>
.baseline-body {
  font-size: 12px;
}

.baseline-subtitle {
  margin: 0 0 10px;
  color: var(--td-text-color-secondary, #666);
  line-height: 1.5;
}

.baseline-filter {
  margin-bottom: 10px;
}

.baseline-table {
  width: 100%;
  border-collapse: collapse;
  max-height: 360px;
}

.baseline-table th {
  text-align: left;
  font-weight: 500;
  color: var(--td-text-color-placeholder, #999);
  padding-bottom: 4px;
  position: sticky;
  top: 0;
  background: var(--td-bg-color-container, #fff);
}

.baseline-table td {
  padding: 3px 8px 3px 0;
  font-family: var(--code-font, monospace);
}

.baseline-empty {
  padding: 16px 0;
  color: var(--td-text-color-placeholder, #999);
}

.link-btn {
  margin-top: 10px;
  border: none;
  background: none;
  color: var(--td-brand-color, #0052d9);
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}
</style>
