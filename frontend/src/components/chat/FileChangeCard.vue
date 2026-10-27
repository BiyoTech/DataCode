<script setup lang="ts">
/**
 * FileChangeCard — Cursor / Claude Code style file-change entry.
 *
 * Renders an `artifact.diff` timeline entry as a compact card showing the file
 * path, a change-type badge, and green +N / red -M line-count badges. The card
 * header toggles an inline unified-diff preview; the "view diff" button opens a
 * read-only side-by-side before/after diff in the editor panel.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
// TDesign icons used via <t-icon> in template
import { useFileStore } from '@/stores/files'
import type { TimelineEntry, ArtifactDiffPayload } from '@/types/agent'

const { t } = useI18n()
const fileStore = useFileStore()

const props = defineProps<{
  entry: TimelineEntry
}>()

const expanded = ref(false)

const payload = computed(() => (props.entry.payload || {}) as ArtifactDiffPayload)
const changeType = computed(() => payload.value.change_type || 'update')
const filePath = computed(() => payload.value.file_path || '')
const linesAdded = computed(() => payload.value.lines_added || 0)
const linesRemoved = computed(() => payload.value.lines_removed || 0)

// Diff lines for the inline preview.
const diffLines = computed(() => (payload.value.diff || '').split('\n'))

// Change-type icon name for <t-icon>.
const typeIcon = computed<string>(() => {
  switch (changeType.value) {
    case 'create': return 'file-add'
    case 'delete': return 'delete'
    default: return 'edit'
  }
})

const typeLabel = computed(() => {
  switch (changeType.value) {
    case 'create': return t('chat.fileChange.created')
    case 'delete': return t('chat.fileChange.deleted')
    default: return t('chat.fileChange.updated')
  }
})

function getDiffLineClass(line: string): string {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-add'
  if (line.startsWith('-') && !line.startsWith('---')) return 'diff-remove'
  if (line.startsWith('@@')) return 'diff-hunk'
  return ''
}

function toggleExpand() {
  expanded.value = !expanded.value
}

// Open a read-only side-by-side diff (before vs after) in the editor panel.
// Accepts both pointer and keyboard triggers: the span is focusable and is
// activated by Enter/Space as well as click.
function openDiff(e: MouseEvent | KeyboardEvent) {
  e.stopPropagation()
  fileStore.openDiffFile(
    filePath.value,
    payload.value.old_content || '',
    payload.value.new_content || '',
    payload.value.language || 'plaintext',
  )
}
</script>

<template>
  <div class="file-change-card" :class="`type-${changeType}`">
    <!-- Header (click to toggle inline diff preview) -->
    <button class="fc-header" @click="toggleExpand">
      <t-icon :name="expanded ? 'chevron-down' : 'chevron-right'" size="12px" class="fc-chevron" />
      <t-icon :name="typeIcon" size="13px" class="fc-type-icon" />
      <span class="fc-badge" :class="`badge-${changeType}`">{{ typeLabel }}</span>
      <span class="fc-path" :title="filePath">{{ filePath }}</span>
      <span class="fc-stats">
        <span v-if="linesAdded > 0" class="stat-added">+{{ linesAdded }}</span>
        <span v-if="linesRemoved > 0" class="stat-removed">-{{ linesRemoved }}</span>
      </span>
      <span class="fc-view-btn" :title="t('chat.fileChange.viewDiff')" role="button" tabindex="0" @click="openDiff" @keydown.enter="openDiff" @keydown.space.prevent="openDiff">
        <t-icon name="swap" size="13px" />
      </span>
    </button>

    <!-- Inline unified-diff preview -->
    <div v-if="expanded && payload.diff" class="fc-diff">
      <pre class="fc-diff-content"><template v-for="(line, idx) in diffLines" :key="idx"><span :class="getDiffLineClass(line)">{{ line }}
</span></template></pre>
    </div>
  </div>
</template>

<style scoped>
.file-change-card {
  align-self: flex-start;
  max-width: 90%;
  width: 90%;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  transition: border-color 0.15s;
}

.file-change-card:hover {
  border-color: var(--accent);
}

/* Header */
.fc-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
}

.fc-chevron {
  flex-shrink: 0;
  color: var(--text-muted);
}

.fc-type-icon {
  flex-shrink: 0;
}

.type-create .fc-type-icon { color: var(--success); }
.type-update .fc-type-icon { color: var(--warning); }
.type-delete .fc-type-icon { color: var(--error); }

.fc-badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.badge-create { background: var(--success-subtle); color: var(--success); }
.badge-update { background: var(--warning-subtle); color: var(--warning); }
.badge-delete { background: var(--error-subtle); color: var(--error); }

.fc-path {
  flex: 1;
  min-width: 0;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: rtl;
  text-align: left;
}

.fc-stats {
  flex-shrink: 0;
  display: flex;
  gap: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 600;
}

.stat-added { color: var(--success); }
.stat-removed { color: var(--error); }

.fc-view-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 3px;
  background: transparent;
  border: none;
  border-radius: 5px;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.12s, background 0.12s;
}

.fc-view-btn:hover {
  color: var(--accent);
  background: var(--bg-hover);
}

/* Inline diff */
.fc-diff {
  border-top: 1px solid var(--border);
  background: var(--bg-surface);
}

.fc-diff-content {
  padding: 8px 10px;
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow-y: auto;
}

.diff-add { color: var(--success); background: var(--success-subtle); }
.diff-remove { color: var(--error); background: var(--error-subtle); }
.diff-hunk { color: var(--accent); margin-top: 4px; }
</style>
