<script setup lang="ts">
/**
 * ToolCallCard — renders a single tool invocation step in the agent timeline.
 *
 * Maps tool names to a friendly icon + label (e.g. read_file -> 📄 Read file),
 * shows the primary argument inline (file path / command), and lets the user
 * expand to inspect the tool result.
 *
 * Four result presentations are supported:
 *  - shell        -> terminal-style output (newlines preserved, ANSI stripped)
 *  - apply_patch  -> editor-style unified diff + "open file" action
 *  - create_file  -> editor-style diff (all lines added) + "open file" action
 *  - write_todo   -> structured todo list (with progress + status icons) instead
 *                    of raw JSON args, so the user sees Cursor-style task list
 *                    in-place; raw payload still available on expansion.
 *  - everything else falls back to a raw arguments / result preview.
 */
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFileStore } from '@/stores/files'
import { useWorkspaceStore } from '@/stores/workspace'
import { notifyError } from '@/utils/notifyError'
import {
  parseShellResults,
  parsePatch,
  parseCreateFile,
  parseTodoItems,
  type ParsedTodoItem,
  type ShellCommandResult,
  type FileDiff,
} from '@/utils/toolOutput'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()
const fileStore = useFileStore()
const workspaceStore = useWorkspaceStore()

const props = defineProps<{
  entry: TimelineEntry
}>()

const isExpanded = ref(false)

const toolName = computed<string>(() => props.entry.payload.tool_name || 'unknown')
const status = computed(() => props.entry.status || 'running')

// Tool name -> TDesign icon mapping. Falls back to a generic tool icon.
const ICON_MAP: Record<string, string> = {
  read_file: 'file',
  create_file: 'edit',
  apply_patch: 'edit',
  write_todo: 'view-list',
  search_code: 'search',
  get_lineage: 'link',
  list_models: 'chart-bar',
  schema_discovery: 'folder',
  shell: 'terminal',
}

const toolIcon = computed(() => ICON_MAP[toolName.value] || 'tools')

// Friendly label via i18n; falls back to the raw tool name when key is missing.
const toolLabel = computed(() => {
  const key = `chat.tools.${toolName.value}`
  const translated = t(key)
  return translated === key ? toolName.value : translated
})

const args = computed<Record<string, any>>(() => props.entry.payload.arguments || {})

// ─────────────────────────── Live progress (tool.progress) ───────────────────────────
// Shown only while the tool is still running; cleared once it ends.
const isRunning = computed(() => status.value === 'running')
const progressMessage = computed<string>(() => props.entry.payload.progress_message || '')
// Clamp the raw 0..1 fraction into a percentage; null when no progress emitted.
const progressPercent = computed<number | null>(() => {
  const raw = props.entry.payload.progress
  if (typeof raw !== 'number' || Number.isNaN(raw)) return null
  return Math.max(0, Math.min(100, Math.round(raw * 100)))
})
const showProgress = computed(
  () => isRunning.value && (progressPercent.value !== null || !!progressMessage.value),
)

// ─────────────────────────── Specialized result detection ───────────────────────────

const isShell = computed(() => toolName.value === 'shell')
const isFileEdit = computed(
  () => toolName.value === 'apply_patch' || toolName.value === 'create_file',
)
const isTodoWrite = computed(() => toolName.value === 'write_todo')

// Parsed shell results (null when the payload is not shell-shaped).
const shellResults = computed<ShellCommandResult[] | null>(() => {
  if (!isShell.value) return null
  return parseShellResults(props.entry.payload.result)
})

// Parsed file diffs for apply_patch / create_file.
const fileDiffs = computed<FileDiff[]>(() => {
  if (toolName.value === 'apply_patch') {
    return parsePatch(args.value.patch || '')
  }
  if (toolName.value === 'create_file') {
    return parseCreateFile(args.value.file_path || '', args.value.content || '')
  }
  return []
})

// ─────────── write_todo: parse the tool args (and the result, if it carries
// a snapshot) into a normalized list. Falls back gracefully to [].
const todoItems = computed<ParsedTodoItem[]>(() => {
  if (!isTodoWrite.value) return []
  // Prefer args (live payload the LLM just decided on); merge in the board
  // snapshot from the result when present so the user sees authoritative ids.
  const fromArgs = parseTodoItems(args.value)
  if (fromArgs.length > 0) return fromArgs
  return parseTodoItems(props.entry.payload.result)
})

const todoProgress = computed(() => {
  const items = todoItems.value
  const total = items.length
  const completed = items.filter((i) => i.status === 'completed').length
  return { total, completed }
})

function statusIconText(status: string): string {
  switch (status) {
    case 'completed': return '\u2713'
    case 'in_progress': return '\u203A'
    case 'pending': return '\u25CB'
    case 'deleted': return '\u2013'
    default: return '\u25CB'
  }
}

// Primary argument shown inline next to the label (path, command, query...).
const primaryArg = computed(() => {
  const a = args.value
  if (isFileEdit.value && fileDiffs.value.length > 0) {
    return fileDiffs.value.map((f) => f.path).join(', ')
  }
  // Shell tool receives a `commands` string array — join for inline display.
  if (isShell.value && Array.isArray(a.commands)) {
    return a.commands.join(' && ')
  }
  // write_todo: show a compact "<n> tasks (<m> done)" hint instead of raw JSON.
  if (isTodoWrite.value) {
    const { total, completed } = todoProgress.value
    if (total > 0) {
      return `${total} \u00B7 ${completed}/${total} ${t('chat.todo.done')}`
    }
  }
  return a.file_path || a.path || a.command || a.query || a.model_name || ''
})

const argsJson = computed(() => {
  const a = args.value
  return Object.keys(a).length > 0 ? JSON.stringify(a, null, 2) : ''
})

const result = computed(() => {
  const r = props.entry.payload.result
  if (r === undefined || r === null || r === '') return ''
  return typeof r === 'string' ? r : JSON.stringify(r, null, 2)
})

const statusIcon = computed(() => {
  switch (status.value) {
    case 'running': return 'loading'
    case 'success': return 'check'
    case 'error': return 'close'
    default: return 'circle'
  }
})

const statusColor = computed(() => {
  switch (status.value) {
    case 'running': return 'var(--info)'
    case 'success': return 'var(--success)'
    case 'error': return 'var(--error)'
    default: return 'var(--text-muted)'
  }
})

// Specialized views (shell / file edits / write_todo) always have something to show.
const hasDetails = computed(
  () => isShell.value || isFileEdit.value || isTodoWrite.value || !!argsJson.value || !!result.value,
)

function toggle() {
  if (hasDetails.value) {
    isExpanded.value = !isExpanded.value
  }
}

/** Open the edited/created file in the editor panel. */
async function openFile(path: string) {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !path) return
  try {
    await fileStore.openFile(ws.path, path)
  } catch (err) {
    notifyError(err, 'Failed to open file')
  }
}
</script>

<template>
  <div class="tool-step">
    <!-- Header line: icon + friendly label + primary arg + status -->
    <div class="tool-line" :class="{ clickable: hasDetails }" @click="toggle">
      <t-icon :name="toolIcon" size="14px" class="tool-icon" />
      <span class="tool-label">{{ toolLabel }}</span>
      <span v-if="primaryArg" class="tool-arg">{{ primaryArg }}</span>
      <t-icon
        :name="statusIcon"
        size="12px"
        class="tool-status"
        :class="{ spin: status === 'running' }"
        :style="{ color: statusColor }"
      />
      <t-icon v-if="hasDetails" name="chevron-right" size="10px" class="tool-chevron" :class="{ expanded: isExpanded }" />
    </div>

    <!-- Live progress while the tool is running -->
    <div v-if="showProgress" class="tool-progress">
      <div v-if="progressPercent !== null" class="progress-track">
        <div class="progress-fill" :style="{ width: progressPercent + '%' }" />
      </div>
      <div class="progress-meta">
        <span v-if="progressMessage" class="progress-msg">{{ progressMessage }}</span>
        <span v-if="progressPercent !== null" class="progress-pct">{{ progressPercent }}%</span>
      </div>
    </div>

    <!-- Expandable details -->
    <div v-if="isExpanded" class="tool-details">
      <!-- ───────── Shell: terminal-style output ───────── -->
      <template v-if="isShell">
        <div
          v-for="(res, idx) in shellResults || []"
          :key="idx"
          class="terminal-block"
        >
          <div class="terminal-cmd"><span class="terminal-prompt">$</span> {{ res.command }}</div>
          <pre v-if="res.stdout" class="terminal-out">{{ res.stdout }}</pre>
          <pre v-if="res.stderr" class="terminal-out terminal-err">{{ res.stderr }}</pre>
          <div class="terminal-meta">
            <span :class="res.exit_code === 0 ? 'exit-ok' : 'exit-fail'">
              exit {{ res.exit_code }}
            </span>
            <span v-if="res.timed_out" class="exit-fail">timed out</span>
          </div>
        </div>
        <!-- Fallback when result is not shell-shaped (e.g. an error string) -->
        <pre v-if="!shellResults && result" class="tool-code">{{ result }}</pre>
      </template>

      <!-- ───────── File edits: editor-style diff ───────── -->
      <template v-else-if="isFileEdit">
        <div v-for="(file, idx) in fileDiffs" :key="idx" class="diff-card">
          <div class="diff-head">
            <span class="diff-path" :title="file.path">{{ file.path }}</span>
            <span class="diff-stats">
              <span v-if="file.added > 0" class="stat-added">+{{ file.added }}</span>
              <span v-if="file.removed > 0" class="stat-removed">-{{ file.removed }}</span>
            </span>
            <button class="diff-open-btn" @click.stop="openFile(file.path)">
              <FileText :size="12" />
              {{ t('chat.fileChange.viewFile') }}
            </button>
          </div>
          <pre class="diff-body"><span
            v-for="(ln, i) in file.lines"
            :key="i"
            class="diff-line"
            :class="`dl-${ln.type}`"
          >{{ ln.text === '' ? ' ' : ln.text }}</span></pre>
        </div>
      </template>

      <!-- ───────── write_todo: structured todo list ───────── -->
      <template v-else-if="isTodoWrite">
        <div v-if="todoItems.length > 0" class="todo-inline">
          <div
            v-for="item in todoItems"
            :key="item.id"
            class="todo-inline-item"
            :class="`todo-inline-item--${item.status}`"
          >
            <span class="todo-inline-icon" :class="`ti-${item.status}`">
              {{ statusIconText(item.status) }}
            </span>
            <span class="todo-inline-body">
              <span class="todo-inline-content" :class="{ 'is-done': item.status === 'completed' }">
                {{ item.content }}
              </span>
              <span
                v-if="item.activeForm && item.activeForm !== item.content"
                class="todo-inline-active-form"
              >
                {{ item.activeForm }}
              </span>
            </span>
          </div>
        </div>
        <div v-else class="tool-section">
          <div class="tool-section-label">{{ t('chat.toolCall.arguments') }}</div>
          <pre class="tool-code">{{ argsJson }}</pre>
        </div>
        <!-- Keep raw result visible for debugging when result carries more than the list. -->
        <div v-if="result" class="tool-section">
          <div class="tool-section-label">{{ t('chat.toolCall.result') }}</div>
          <pre class="tool-code">{{ result }}</pre>
        </div>
      </template>

      <!-- ───────── Generic fallback ───────── -->
      <template v-else>
        <div v-if="argsJson" class="tool-section">
          <div class="tool-section-label">{{ t('chat.toolCall.arguments') }}</div>
          <pre class="tool-code">{{ argsJson }}</pre>
        </div>
        <div v-if="result" class="tool-section">
          <div class="tool-section-label">{{ t('chat.toolCall.result') }}</div>
          <pre class="tool-code">{{ result }}</pre>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.tool-step {
  align-self: flex-start;
  max-width: 90%;
  width: 100%;
}

.tool-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--text-primary);
  transition: background 0.15s ease;
}

.tool-line.clickable {
  cursor: pointer;
}

.tool-line.clickable:hover {
  background: var(--bg-hover);
}

.tool-icon {
  flex-shrink: 0;
  color: var(--text-secondary);
}

.tool-label {
  font-weight: 500;
  flex-shrink: 0;
}

.tool-arg {
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-status {
  margin-left: auto;
  flex-shrink: 0;
}

.tool-status.spin {
  display: inline-block;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.tool-chevron {
  color: var(--text-secondary);
  transition: transform 0.2s ease;
  flex-shrink: 0;
}

.tool-chevron.expanded {
  transform: rotate(90deg);
}

.tool-details {
  margin: 4px 0 4px 28px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* ───────── Live progress bar (tool.progress) ───────── */
.tool-progress {
  margin: 2px 0 4px 28px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.progress-track {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-primary);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--info);
  border-radius: 2px;
  transition: width 0.3s ease;
}

.progress-meta {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}

.progress-msg {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-pct {
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.tool-section-label {
  font-size: 11px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.tool-code {
  font-size: 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  overflow-x: auto;
  color: var(--text-primary);
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

/* ───────── Terminal-style shell output ───────── */
.terminal-block {
  background: #1e1e1e;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}

.terminal-cmd {
  padding: 6px 12px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  color: #d4d4d4;
  background: #252526;
  border-bottom: 1px solid #333;
  white-space: pre-wrap;
  word-break: break-all;
}

.terminal-prompt {
  color: #4ec9b0;
  font-weight: 700;
  margin-right: 4px;
}

.terminal-out {
  margin: 0;
  padding: 8px 12px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  line-height: 1.45;
  color: #d4d4d4;
  background: #1e1e1e;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow-y: auto;
}

.terminal-err {
  color: #f48771;
}

.terminal-meta {
  display: flex;
  gap: 10px;
  padding: 4px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  background: #252526;
  border-top: 1px solid #333;
}

.exit-ok { color: #4ec9b0; }
.exit-fail { color: #f48771; }

/* ───────── Editor-style file diff ───────── */
.diff-card {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}

.diff-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}

.diff-path {
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

.diff-stats {
  flex-shrink: 0;
  display: flex;
  gap: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 600;
}

.stat-added { color: var(--success); }
.stat-removed { color: var(--error); }

.diff-open-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--accent);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}

.diff-open-btn:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
}

.diff-body {
  margin: 0;
  padding: 0;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  line-height: 1.5;
  max-height: 320px;
  overflow: auto;
}

.diff-line {
  display: block;
  padding: 0 10px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}

.dl-add {
  background: var(--success-subtle);
  color: var(--success);
}

.dl-remove {
  background: var(--error-subtle);
  color: var(--error);
}

.dl-hunk {
  color: var(--accent);
  background: var(--bg-secondary);
}

.dl-context {
  color: var(--text-secondary);
}

/* ===== Inline todo list (write_todo tool) ===== */
.todo-inline {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 0;
}

.todo-inline-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  line-height: 1.4;
}

.todo-inline-icon {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: bold;
  border-radius: 50%;
}

.ti-completed { color: var(--success); }
.ti-in_progress { color: var(--info); }
.ti-pending { color: var(--text-secondary); }
.ti-deleted { color: var(--error); }

.todo-inline-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.todo-inline-content {
  color: var(--text-primary);
  word-break: break-word;
}

.todo-inline-content.is-done {
  text-decoration: line-through;
  color: var(--text-secondary);
}

.todo-inline-active-form {
  color: var(--text-secondary);
  font-size: 12px;
  font-style: italic;
  line-height: 1.3;
  word-break: break-word;
}
</style>
