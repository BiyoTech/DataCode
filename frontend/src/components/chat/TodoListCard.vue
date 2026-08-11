<script setup lang="ts">
/**
 * TodoListCard — renders a Cursor-style task list card in the agent timeline.
 *
 * Displays a compact, interactive task list showing:
 * - Progress header (e.g. "任务清单 3/5 已完成")
 * - Each item with status icon and optional strikethrough for completed items
 * - Color-coded status indicators
 *
 * Accepts several input shapes via `parseTodoItems`:
 *  - `todo.update` event payload: { tasks, total, completed, in_progress }
 *  - `write_todo` tool-call args: { todos: "[...]" | [...] , merge }
 *  - `write_todo` tool result string
 *  - LLM text output: { tool: "write_todo", items: [{content, status, activeForm}, ...] }
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { parseTodoItems, type ParsedTodoItem } from '@/utils/toolOutput'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()

const props = defineProps<{
  entry: TimelineEntry
}>()

// Normalize every supported payload shape into a single ParsedTodoItem[].
const tasks = computed<ParsedTodoItem[]>(() => {
  const payload = props.entry.payload || {}
  // Prefer explicit arrays from the structured payloads; fall back to the
  // generic parser so tool-call args / LLM-emitted text all funnel into the
  // same renderer.
  const items = parseTodoItems(payload)
  if (items.length > 0) return items
  return []
})

const total = computed(
  () => (props.entry.payload.total as number | undefined) ?? tasks.value.length,
)
const completed = computed(
  () =>
    (props.entry.payload.completed as number | undefined) ??
    tasks.value.filter((t_) => t_.status === 'completed').length,
)

// Status icon mapping
function statusIcon(status: string): string {
  switch (status) {
    case 'completed':
      return '\u2713'
    case 'in_progress':
      return '\u203A'
    case 'pending':
      return '\u25CB'
    case 'deleted':
      return '\u2013'
    default:
      return '\u25CB'
  }
}
</script>

<template>
  <div class="todo-card">
    <!-- Header with progress -->
    <div class="todo-header">
      <span class="todo-icon">☰</span>
      <span class="todo-title">{{ t('chat.todo.title') }}</span>
      <span class="todo-progress">{{ completed }}/{{ total }} {{ t('chat.todo.done') }}</span>
    </div>

    <!-- Task list -->
    <div v-if="tasks.length > 0" class="todo-list">
      <div
        v-for="task in tasks"
        :key="task.id"
        class="todo-item"
        :class="`todo-item--${task.status}`"
      >
        <span class="todo-item-icon" :class="`icon--${task.status}`">
          {{ statusIcon(task.status) }}
        </span>
        <span class="todo-item-body">
          <span class="todo-item-content" :class="{ 'is-done': task.status === 'completed' }">
            {{ task.content }}
          </span>
          <!--
            activeForm is the LLM's "currently doing X" line, distinct from the
            static content. Only render when it adds information beyond content.
          -->
          <span
            v-if="task.activeForm && task.activeForm !== task.content"
            class="todo-item-active-form"
          >
            {{ task.activeForm }}
          </span>
        </span>
      </div>
    </div>
    <div v-else class="todo-empty">{{ t('chat.todo.empty') }}</div>
  </div>
</template>

<style scoped>
.todo-card {
  align-self: flex-start;
  max-width: 90%;
  width: 100%;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
  font-size: 13px;
}

.todo-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  color: var(--text-primary);
}

.todo-icon {
  font-size: 14px;
  opacity: 0.7;
}

.todo-title {
  font-weight: 600;
  font-size: 13px;
}

.todo-progress {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-secondary);
}

.todo-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.todo-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 0;
}

.todo-item-icon {
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

.todo-item-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.icon--completed {
  color: var(--success);
}

.icon--in_progress {
  color: var(--info);
  animation: pulse 1.5s ease-in-out infinite;
}

.icon--pending {
  color: var(--text-secondary);
}

.icon--deleted {
  color: var(--error);
}

.todo-item-content {
  color: var(--text-primary);
  line-height: 1.4;
  word-break: break-word;
}

.todo-item-content.is-done {
  text-decoration: line-through;
  color: var(--text-secondary);
}

.todo-item-active-form {
  color: var(--text-secondary);
  font-size: 12px;
  font-style: italic;
  line-height: 1.3;
  word-break: break-word;
}

.todo-empty {
  color: var(--text-secondary);
  font-size: 12px;
  font-style: italic;
  padding: 4px 0;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
</style>
