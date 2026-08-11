<script setup lang="ts">
/**
 * PlanLintResults - Renders the "Linter errors" panel for plan preview/apply.
 *
 * Shown when a plan is blocked by linter violations. Each row mirrors the
 * SQLMesh CLI's per-file lint output (file path + rule name + message) and
 * groups violations by file so the user can scan the offending models.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '@/stores/plan'
import type { LintViolation } from '@/types/plan'

const { t } = useI18n()
const planStore = usePlanStore()

// All violations expanded by default — the user came here to see them.
const expandedFiles = ref<Set<string>>(new Set())
const expanded = ref(true)

const lint = computed(() => planStore.lintResults)

// Auto-expand every offending file whenever a new set of lint results arrives
// so the user immediately sees the per-file diagnostics without extra clicks.
watch(
  lint,
  (lr) => {
    if (!lr) return
    const files = new Set<string>()
    for (const v of lr.by_file) files.add(v.file_path || '(no file)')
    expandedFiles.value = files
    expanded.value = true
  },
  { immediate: true },
)

interface FileGroup {
  filePath: string
  modelNames: string[]
  violations: LintViolation[]
  errors: number
  warnings: number
}

const grouped = computed<FileGroup[]>(() => {
  const lr = lint.value
  if (!lr) return []
  const groups = new Map<string, FileGroup>()
  for (const v of lr.by_file) {
    const key = v.file_path || '__no_file__'
    let g = groups.get(key)
    if (!g) {
      g = {
        filePath: v.file_path || '(no file)',
        modelNames: [],
        violations: [],
        errors: 0,
        warnings: 0,
      }
      groups.set(key, g)
    }
    g.violations.push(v)
    if (v.severity === 'error') g.errors += 1
    else if (v.severity === 'warning') g.warnings += 1
    if (v.model_name && !g.modelNames.includes(v.model_name)) {
      g.modelNames.push(v.model_name)
    }
  }
  return Array.from(groups.values()).sort((a, b) => b.errors - a.errors || a.filePath.localeCompare(b.filePath))
})

function fileName(path: string): string {
  if (!path) return '(no file)'
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

function toggleAll() {
  expanded.value = !expanded.value
}

function toggleFile(filePath: string) {
  if (expandedFiles.value.has(filePath)) {
    expandedFiles.value.delete(filePath)
  } else {
    expandedFiles.value.add(filePath)
  }
}

function getRelativePath(absolutePath: string): string {
  if (!absolutePath) return ''
  // Strip common workspace prefixes for a friendlier display.
  const idx = absolutePath.indexOf('/models/')
  if (idx >= 0) return absolutePath.slice(idx + 1)
  return absolutePath
}
</script>

<template>
  <div v-if="lint && lint.total > 0" class="lint-results">
    <div class="fail-banner">
      <t-icon name="error-circle" size="14px" />
      <span>{{ t('plan.lintFailed') }}</span>
      <span class="banner-counts">
        {{ lint.errors }} {{ t('plan.lintErrors') }}<span v-if="lint.warnings > 0"> · {{ lint.warnings }} {{ t('plan.lintWarnings') }}</span>
      </span>
    </div>

    <div class="fail-card">
      <button class="fail-card-header" @click="toggleAll">
        <t-icon name="minus-circle" size="14px" class="header-icon" />
        <span>{{ t('plan.lintViolationsTitle') }}</span>
        <t-icon :name="expanded ? 'chevron-down' : 'chevron-right'" size="14px" class="chevron" />
      </button>

      <div v-if="expanded" class="fail-card-body">
        <div
          v-for="group in grouped"
          :key="group.filePath"
          class="file-group"
        >
          <button class="file-group-header" @click="toggleFile(group.filePath)">
            <t-icon
              :name="expandedFiles.has(group.filePath) ? 'chevron-down' : 'chevron-right'"
              size="12px"
            />
            <span class="file-name">{{ fileName(group.filePath) }}</span>
            <span class="file-relative">{{ getRelativePath(group.filePath) }}</span>
            <span class="file-counts">
              <span v-if="group.errors > 0" class="count-error">{{ group.errors }}</span>
              <span v-if="group.warnings > 0" class="count-warning">{{ group.warnings }}</span>
            </span>
          </button>

          <div
            v-if="expandedFiles.has(group.filePath)"
            class="violation-list"
          >
            <div class="violation-list-meta">
              <span v-for="m in group.modelNames" :key="m" class="model-chip">{{ m }}</span>
            </div>
            <div
              v-for="(v, idx) in group.violations"
              :key="idx"
              class="violation-row"
              :class="{ 'is-warning': v.severity === 'warning' }"
            >
              <span class="violation-rule">{{ v.rule || 'lint' }}</span>
              <span class="violation-pos" v-if="v.line">
                {{ v.line }}<template v-if="v.column">:{{ v.column }}</template>
              </span>
              <span class="violation-msg">{{ v.message }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lint-results {
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

.banner-counts {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono, monospace);
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

.file-group {
  background: var(--bg-primary);
  border-radius: 6px;
  overflow: hidden;
}

.file-group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  background: var(--bg-hover);
  border: none;
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.file-name {
  font-weight: 600;
  font-family: var(--font-mono, monospace);
}

.file-relative {
  color: var(--text-muted);
  font-size: 10px;
  font-family: var(--font-mono, monospace);
  word-break: break-all;
  flex: 1;
}

.file-counts {
  display: flex;
  gap: 4px;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

.count-error {
  color: var(--error);
  background: var(--error-subtle);
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 600;
}

.count-warning {
  color: var(--warning);
  background: var(--warning-subtle);
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 600;
}

.violation-list {
  padding: 8px 10px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.violation-list-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding-bottom: 4px;
  border-bottom: 1px dashed var(--border);
}

.model-chip {
  font-size: 10px;
  font-family: var(--font-mono, monospace);
  background: var(--bg-hover);
  color: var(--text-muted);
  padding: 1px 6px;
  border-radius: 8px;
}

.violation-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 11px;
  font-family: var(--font-mono, monospace);
  color: var(--error);
  line-height: 1.5;
  word-break: break-word;
}

.violation-row.is-warning {
  color: var(--warning);
}

.violation-rule {
  flex-shrink: 0;
  font-weight: 600;
  background: var(--error-subtle);
  padding: 0 6px;
  border-radius: 4px;
  white-space: nowrap;
}

.is-warning .violation-rule {
  background: var(--warning-subtle);
}

.violation-pos {
  flex-shrink: 0;
  color: var(--text-muted);
  min-width: 40px;
}

.violation-msg {
  flex: 1;
}
</style>
