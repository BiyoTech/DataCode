<script setup lang="ts">
/**
 * MessageBubble — renders a chat message (user or assistant).
 *
 * Assistant content is rendered as rich, sanitized Markdown (code blocks, lists,
 * tables, links, KaTeX math, and ```echarts / ```mermaid diagrams) via the shared
 * useRichMarkdown composable — the same renderer used by MarkdownPreview. User
 * content stays plain text to avoid injection and because users type literal text.
 *
 * Special case: some agents emit a single line of structured JSON instead of
 * calling a tool function (e.g. design mode outputting `{"tool":"write_todo",
 * "items":[...]}` when `write_todo` isn't registered). When the assistant
 * payload is detected as such, the bubble short-circuits the markdown renderer
 * and shows a structured TodoListCard instead — this is the same recovery path
 * Cursor-style clients use so the user never sees raw JSON.
 *
 * A blinking cursor is shown while streaming.
 */
import { computed, ref, watch, nextTick } from 'vue'
import { formatRelativeTime } from '@/utils/timezone'
import { useRichMarkdown } from '@/composables/useRichMarkdown'
import { useNow } from '@/composables/useNow'
import { parseTodoItems, type ParsedTodoItem } from '@/utils/toolOutput'
import TodoListCard from '@/components/chat/TodoListCard.vue'
import type { TimelineEntry } from '@/types/agent'

const { render, renderExtensions } = useRichMarkdown()

// Drive periodic re-render of `relativeTime` so "刚刚" / "3 minutes ago" stays
// fresh without each bubble owning a timer.
const now = useNow()

const props = defineProps<{
  entry: TimelineEntry
}>()

const bubbleEl = ref<HTMLElement | null>(null)

const isUser = computed(() => props.entry.payload.role === 'user')
const isStreaming = computed(() => props.entry.type === 'message.delta')

const content = computed(() => {
  if (props.entry.type === 'message.delta') {
    return props.entry.accumulatedText || ''
  }
  return props.entry.payload.content || ''
})

/**
 * Detect a top-level JSON tool-call emitted as text by the LLM. The marker
 * shape is `{"tool": "<name>", ...}`; only one such tool is currently routed
 * (write_todo) but the detector is structured so additional tools can plug in.
 */
const detectedTool = computed<{ name: string; data: Record<string, unknown> } | null>(() => {
  if (isUser.value || isStreaming.value) return null
  const raw = content.value.trim()
  if (!raw.startsWith('{') || !raw.endsWith('}')) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const obj = parsed as Record<string, unknown>
  const toolName = obj.tool
  if (typeof toolName !== 'string' || !toolName) return null
  // Only route known tools; everything else falls through to the markdown view.
  if (toolName !== 'write_todo') return null
  return { name: toolName, data: obj }
})

// Normalized todo items for the detected write_todo payload. Built once so the
// template can render <TodoListCard> with stable shape.
const detectedTodoItems = computed<ParsedTodoItem[]>(() => {
  const det = detectedTool.value
  if (!det) return []
  return parseTodoItems(det.data)
})

const detectedTodoEntry = computed<TimelineEntry | null>(() => {
  const det = detectedTool.value
  if (!det || detectedTodoItems.value.length === 0) return null
  // Synthesize a todo.update-shaped TimelineEntry so TodoListCard renders it
  // without having to know about the LLM-text variant.
  return {
    id: props.entry.id,
    type: 'todo.update',
    timestamp: props.entry.timestamp,
    payload: {
      tasks: detectedTodoItems.value,
      total: detectedTodoItems.value.length,
      completed: detectedTodoItems.value.filter((i) => i.status === 'completed').length,
      in_progress: detectedTodoItems.value.filter((i) => i.status === 'in_progress').length,
    },
  }
})

// Rendered markdown HTML for assistant messages (sanitized).
const renderedHtml = computed(() => (isUser.value ? '' : render(content.value)))

// Hydrate echarts/mermaid blocks only once the message is complete. During
// streaming the fenced blocks are often incomplete (invalid JSON / unclosed
// fences), so we defer to avoid flicker and wasted chart initialization.
watch(
  [renderedHtml, isStreaming],
  async () => {
    if (isUser.value || isStreaming.value) return
    if (detectedTool.value) return // structured view doesn't need markdown hydration
    await nextTick()
    await renderExtensions(bubbleEl.value)
  },
  { immediate: true },
)

const agentName = computed(() => props.entry.payload.agent_name || '')

// Recomputed whenever the global `now` tick changes so the relative label
// ("3 minutes ago") stays current over time.
const relativeTime = computed(() => {
  // Touch `now` to register the reactive dependency.
  void now.value
  return formatRelativeTime(props.entry.timestamp)
})
</script>

<template>
  <div :class="['message-bubble', isUser ? 'message-user' : 'message-assistant']">
    <div v-if="!isUser && agentName" class="agent-badge">
      {{ agentName }}
    </div>
    <div class="bubble-content">
      <!--
        Branch 1: LLM emitted a single JSON tool-call as text → render the
        structured card so the user never sees raw JSON. This recovers the
        write_todo / design-agent mismatch where the tool isn't registered.
      -->
      <TodoListCard
        v-if="detectedTodoEntry"
        :entry="detectedTodoEntry"
      />

      <!-- Branch 2: normal user/assistant text path. -->
      <template v-else>
        <!-- User: plain text. Assistant: sanitized rich markdown. -->
        <div v-if="isUser" class="bubble-text">{{ content }}</div>
        <div
          v-else
          ref="bubbleEl"
          class="bubble-text markdown-body"
          :class="{ streaming: isStreaming }"
          v-html="renderedHtml"
        ></div>
        <span v-if="isStreaming" class="cursor-blink">|</span>
        <div class="bubble-time">{{ relativeTime }}</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.message-bubble {
  max-width: 80%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.message-user {
  align-self: flex-end;
}

.message-assistant {
  align-self: flex-start;
}

.agent-badge {
  font-size: 11px;
  color: var(--text-secondary);
  padding-left: 4px;
}

.message-user .bubble-content {
  background: var(--accent);
  color: var(--accent-foreground);
  border-radius: 16px 16px 4px 16px;
  padding: 12px 16px;
}

.message-assistant .bubble-content {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 16px 16px 16px 4px;
  padding: 12px 16px;
  color: var(--text-primary);
}

.bubble-text {
  white-space: pre-wrap;
  line-height: 1.6;
  font-size: 14px;
}

.bubble-text.markdown-body {
  white-space: normal;
}

.cursor-blink {
  animation: blink 1s step-end infinite;
  color: var(--accent);
  font-weight: bold;
}

@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}

.bubble-time {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
}

/* ===== Markdown content styling ===== */
.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(pre) {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: 8px 0;
}
.markdown-body :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12.5px;
}
.markdown-body :deep(:not(pre) > code) {
  background: var(--bg-primary);
  border-radius: 4px;
  padding: 1px 5px;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 6px 0;
  padding-left: 20px;
}
.markdown-body :deep(li) {
  margin: 2px 0;
}
.markdown-body :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 10px 0 6px;
  font-size: 1.05em;
  font-weight: 600;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 12.5px;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--border);
  padding: 4px 8px;
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--border);
  margin: 8px 0;
  padding-left: 10px;
  color: var(--text-secondary);
}

/* ===== Rich extensions (echarts / mermaid / katex) ===== */
/* Charts and diagrams size to the bubble width instead of a fixed width. */
.markdown-body :deep(.md-echarts) {
  width: 100%;
  min-height: 360px;
  margin: 8px 0;
}
.markdown-body :deep(.md-diagram) {
  display: flex;
  justify-content: center;
  margin: 8px 0;
  overflow-x: auto;
}
.markdown-body :deep(.md-diagram svg) {
  max-width: 100%;
}
.markdown-body :deep(.katex) {
  font-size: 1.02em;
}
.markdown-body :deep(.katex-display) {
  margin: 8px 0;
  overflow-x: auto;
}
.markdown-body :deep(.katex-error),
.markdown-body :deep(.echarts-error),
.markdown-body :deep(.mermaid-error) {
  color: var(--error);
  font-size: 12.5px;
}
</style>
