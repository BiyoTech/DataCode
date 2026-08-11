<script setup lang="ts">
/**
 * Shared chat timeline panel — timeline rendering, header, composer shell.
 * Scenario-specific mention pickers and mode selectors are injected via slots.
 */
import { ref, computed, nextTick, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ErrorCard from '@/components/chat/ErrorCard.vue'
import SystemNoticeCard from '@/components/chat/SystemNoticeCard.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import ToolCallCard from '@/components/chat/ToolCallCard.vue'
import AgentHandoff from '@/components/chat/AgentHandoff.vue'
import TimelineStep from '@/components/chat/TimelineStep.vue'
import FileChangeCard from '@/components/chat/FileChangeCard.vue'
import TodoListCard from '@/components/chat/TodoListCard.vue'
import ApprovalCard from '@/components/chat/ApprovalCard.vue'
import SessionPanel, { type SessionStoreLike } from '@/components/chat/SessionPanel.vue'
import IconButton from '@/components/common/IconButton.vue'
import type { TimelineEntry } from '@/types/agent'

export interface ChatTimelineStoreLike extends SessionStoreLike {
  timeline: TimelineEntry[]
  isThinking: boolean
  hasPendingApproval: boolean
  selectedModelId: string
  availableModels: { id: string; name: string; provider: string }[]
  setModel: (modelId: string) => void
  cancelChat: () => void
  sendApproval: (requestId: string, approved: boolean, reason?: string) => Promise<void>
}

const props = defineProps<{
  store: ChatTimelineStoreLike
  title: string
  sessionLabelNamespace: string
  placeholder: string
  thinkingLabel: string
  sendLabel: string
  cancelLabel: string
}>()

const { t } = useI18n()

const inputText = defineModel<string>('inputText', { default: '' })

const emit = defineEmits<{
  (e: 'send'): void
  (e: 'input', event: Event): void
  (e: 'keydown', event: KeyboardEvent): void
}>()

const messagesContainer = ref<HTMLElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

defineExpose({ textareaRef, messagesContainer })

const VISIBLE_TYPES = new Set([
  'message.complete',
  'message.delta',
  'tool.start',
  'agent.delegate',
  'thought.plan',
  'thought.update',
  'artifact.created',
  'artifact.diff',
  'artifact.image',
  'artifact.chart',
  'task.created',
  'task.running',
  'task.completed',
  'task.failed',
  'todo.update',
  'agent.finish',
  'human.required',
  'system.error',
  'system.warning',
])

const displayEntries = computed(() =>
  props.store.timeline.filter((e: TimelineEntry) => VISIBLE_TYPES.has(e.type)),
)

// "Busy" covers both active generation and paused-awaiting-approval, so the
// composer never falls back to its idle/"completed" look while a run is still
// in progress or blocked on a human decision.
const isBusy = computed(() => props.store.isThinking || props.store.hasPendingApproval)

// While only awaiting approval (not actively generating), show a dedicated
// label instead of the generic "thinking" text.
const busyLabel = computed(() =>
  props.store.hasPendingApproval && !props.store.isThinking
    ? t('chat.thinking.awaitingApproval')
    : props.thinkingLabel,
)

function isMessageEntry(entry: TimelineEntry): boolean {
  return entry.type === 'message.complete' || entry.type === 'message.delta'
}
function isToolEntry(entry: TimelineEntry): boolean {
  return entry.type === 'tool.start'
}
function isHandoffEntry(entry: TimelineEntry): boolean {
  return entry.type === 'agent.delegate'
}
function isStepEntry(entry: TimelineEntry): boolean {
  return (
    entry.type === 'thought.plan' ||
    entry.type === 'thought.update' ||
    entry.type === 'artifact.created' ||
    entry.type === 'artifact.image' ||
    entry.type === 'artifact.chart' ||
    entry.type === 'task.created' ||
    entry.type === 'task.running' ||
    entry.type === 'task.completed' ||
    entry.type === 'task.failed' ||
    entry.type === 'agent.finish'
  )
}
function isFileChangeEntry(entry: TimelineEntry): boolean {
  return entry.type === 'artifact.diff'
}
function isTodoEntry(entry: TimelineEntry): boolean {
  return entry.type === 'todo.update'
}
function isErrorEntry(entry: TimelineEntry): boolean {
  return entry.type === 'system.error'
}
function isWarningEntry(entry: TimelineEntry): boolean {
  return entry.type === 'system.warning'
}
function isApprovalEntry(entry: TimelineEntry): boolean {
  return entry.type === 'human.required'
}

function onModelChange(e: Event) {
  const target = e.target as HTMLSelectElement
  props.store.setModel(target.value)
}

// Sticky-bottom auto-scroll: only follow new content when the user is already
// near the bottom, so scrolling up to read history is never interrupted.
const NEAR_BOTTOM_THRESHOLD = 120
const stickToBottom = ref(true)

function isNearBottom(): boolean {
  const el = messagesContainer.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_THRESHOLD
}

function onMessagesScroll() {
  stickToBottom.value = isNearBottom()
}

function scrollToBottom() {
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
  }
}

// New timeline entries (including the user's own message) always pin to bottom.
watch(
  () => props.store.timeline.length,
  async () => {
    stickToBottom.value = true
    await nextTick()
    scrollToBottom()
  },
)

// Streaming deltas mutate the last bubble's text without changing the timeline
// length, so track that text to keep the view scrolled while the user stays at
// the bottom.
const streamingText = computed(() => {
  const list = displayEntries.value
  const last = list[list.length - 1]
  return last && last.type === 'message.delta' ? last.accumulatedText || '' : ''
})

watch(streamingText, async () => {
  if (!stickToBottom.value) return
  await nextTick()
  scrollToBottom()
})

// Auto-grow the composer textarea with its content, capped by CSS max-height.
function autoResizeTextarea() {
  const ta = textareaRef.value
  if (!ta) return
  ta.style.height = 'auto'
  ta.style.height = `${ta.scrollHeight}px`
}

watch(inputText, () => {
  nextTick(autoResizeTextarea)
})
</script>

<template>
  <div class="chat-timeline-panel">
    <div class="chat-header">
      <span class="chat-title">{{ title }}</span>
      <div class="header-actions">
        <IconButton
          class="header-btn"
          :label="t(`${sessionLabelNamespace}.newChat`)"
          @click="store.createNewSession()"
        >
          <t-icon name="add" size="16px" />
        </IconButton>
        <IconButton
          class="header-btn"
          :label="t(`${sessionLabelNamespace}.title`)"
          @click="store.toggleSessionPanel()"
        >
          <t-icon name="view-list" size="16px" />
        </IconButton>
      </div>
    </div>

    <SessionPanel
      v-if="store.sessionPanelOpen"
      :store="store"
      :label-namespace="sessionLabelNamespace"
    />

    <div class="chat-messages" ref="messagesContainer" @scroll="onMessagesScroll">
      <slot v-if="store.timeline.length === 0" name="welcome" />

      <template v-for="entry in displayEntries" :key="entry.id">
        <MessageBubble v-if="isMessageEntry(entry)" :entry="entry" />
        <ToolCallCard v-else-if="isToolEntry(entry)" :entry="entry" />
        <AgentHandoff v-else-if="isHandoffEntry(entry)" :entry="entry" />
        <ApprovalCard
          v-else-if="isApprovalEntry(entry)"
          :entry="entry"
          :on-approve="store.sendApproval"
        />
        <FileChangeCard v-else-if="isFileChangeEntry(entry)" :entry="entry" />
        <TodoListCard v-else-if="isTodoEntry(entry)" :entry="entry" />
        <TimelineStep v-else-if="isStepEntry(entry)" :entry="entry" />
        <SystemNoticeCard v-else-if="isWarningEntry(entry)" :entry="entry" />
        <ErrorCard v-else-if="isErrorEntry(entry)" :entry="entry" />
      </template>

      <div v-if="isBusy" class="thinking-row">
        <div class="thinking-bubble">
          <span class="dot-pulse" />
          <span class="dot-pulse" />
          <span class="dot-pulse" />
          <span class="thinking-text">{{ busyLabel }}</span>
        </div>
      </div>
    </div>

    <div class="chat-input">
      <slot name="mention-popup" />

      <div class="composer">
        <textarea
          ref="textareaRef"
          :value="inputText"
          :placeholder="placeholder"
          @input="emit('input', $event)"
          @keydown="emit('keydown', $event)"
          rows="2"
        />
        <div class="composer-footer">
          <div class="input-selectors">
            <slot name="selectors-extra" />
            <select
              :value="store.selectedModelId"
              class="model-select-inline"
              @change="onModelChange"
            >
              <option v-for="m in store.availableModels" :key="m.id" :value="m.id">
                {{ m.name }}
              </option>
            </select>
          </div>

          <t-button
            v-if="isBusy"
            theme="default"
            variant="outline"
            size="small"
            @click="store.cancelChat()"
          >
            <template #icon><t-icon name="stop-circle" /></template>
            {{ cancelLabel }}
          </t-button>
          <t-button
            v-else
            theme="primary"
            size="small"
            :disabled="!inputText.trim()"
            @click="emit('send')"
          >
            <template #icon><t-icon name="send" /></template>
            {{ sendLabel }}
          </t-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-timeline-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--td-bg-color-container);
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--td-component-stroke);
  min-height: 44px;
  flex-shrink: 0;
}

.chat-title {
  font-size: var(--td-font-size-title-small);
  font-weight: 600;
  color: var(--td-text-color-primary);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.header-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: var(--td-radius-default);
  background: transparent;
  color: var(--td-text-color-secondary);
  cursor: pointer;
  transition: all 0.15s;
}

.header-btn:hover {
  background: var(--td-bg-color-container-hover);
  color: var(--td-text-color-primary);
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  scrollbar-width: thin;
  scrollbar-color: var(--td-scrollbar-color) transparent;
}

.thinking-row {
  align-self: flex-start;
  max-width: 90%;
}

.thinking-bubble {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--td-bg-color-secondarycontainer);
  border: 1px solid var(--td-component-stroke);
  border-radius: 16px 16px 16px 4px;
}

.dot-pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--td-brand-color);
  animation: pulse 1.2s infinite ease-in-out;
}

.dot-pulse:nth-child(2) { animation-delay: 0.2s; }
.dot-pulse:nth-child(3) { animation-delay: 0.4s; }

.thinking-text {
  margin-left: 4px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

@keyframes pulse {
  0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
  40% { opacity: 1; transform: scale(1); }
}

.chat-input {
  position: relative;
  border-top: 1px solid var(--td-component-stroke);
  padding: 10px 14px 12px;
  background: var(--td-bg-color-container);
  flex-shrink: 0;
}

.composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--td-bg-color-secondarycontainer);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 10px 12px;
  transition: border-color 0.2s;
}

.composer:focus-within {
  border-color: var(--td-brand-color);
}

.composer textarea {
  width: 100%;
  background: transparent;
  border: none;
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-medium);
  line-height: 1.6;
  resize: none;
  outline: none;
  font-family: inherit;
  min-height: 44px;
  max-height: 160px;
}

.composer textarea::placeholder {
  color: var(--td-text-color-placeholder);
}

.composer-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.input-selectors {
  display: flex;
  align-items: center;
  gap: 8px;
}

.model-select-inline {
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-small);
  color: var(--td-text-color-secondary);
  font-size: var(--td-font-size-body-small);
  padding: 4px 8px;
  cursor: pointer;
  max-width: 160px;
  outline: none;
  transition: border-color 0.15s;
}

.model-select-inline:hover,
.model-select-inline:focus {
  border-color: var(--td-brand-color);
}
</style>
