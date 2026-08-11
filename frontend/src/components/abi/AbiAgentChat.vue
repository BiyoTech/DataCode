<script setup lang="ts">
/**
 * AbiAgentChat — ChatBI conversation panel for the A-BI module.
 */
import { ref, computed, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useChatbiStore } from '@/stores/chatbi'
import { useChatbiMention, type MentionItem, type MentionGroup } from '@/composables/useChatbiMention'
import ChatTimelinePanel from '@/components/chat/ChatTimelinePanel.vue'
import ExecutionStylePicker from '@/components/chat/ExecutionStylePicker.vue'

const { t } = useI18n()
const chatbiStore = useChatbiStore()
const { loading: mentionLoading, ensureLoaded: ensureMentionLoaded, search: searchMentions } =
  useChatbiMention()

const inputText = ref('')
const panelRef = ref<InstanceType<typeof ChatTimelinePanel> | null>(null)
const mentionActive = ref(false)
const mentionQuery = ref('')
const mentionStartIdx = ref(-1)
const mentionSelectedIdx = ref(0)
const mentionGroups = ref<MentionGroup[]>([])
const collectedMentions = ref<MentionItem[]>([])

// Flattened view of mentionGroups with group metadata attached, precomputed so
// the v-for highlight check is O(1) instead of O(n²) per render.
interface FlatMentionItem {
  item: MentionItem
  group: MentionGroup
  flatIdx: number
}
const flatMentionItems = computed<FlatMentionItem[]>(() => {
  const out: FlatMentionItem[] = []
  let idx = 0
  for (const group of mentionGroups.value) {
    for (const item of group.items) {
      out.push({ item, group, flatIdx: idx++ })
    }
  }
  return out
})

// Debounce mention search so each keystroke doesn't synchronously re-scan the
// full table/knowledge corpus. 120ms is imperceptible to users but coalesces
// rapid typing.
let mentionSearchTimer: ReturnType<typeof setTimeout> | null = null
function scheduleMentionSearch(query: string) {
  if (mentionSearchTimer) clearTimeout(mentionSearchTimer)
  mentionSearchTimer = setTimeout(() => {
    mentionSearchTimer = null
    ensureMentionLoaded().then(() => {
      mentionGroups.value = searchMentions(query)
    })
  }, 120)
}

onMounted(async () => {
  chatbiStore.fetchModels()
  await chatbiStore.initSession()
})

onBeforeUnmount(() => {
  if (mentionSearchTimer) {
    clearTimeout(mentionSearchTimer)
    mentionSearchTimer = null
  }
  chatbiStore.disconnectSession()
})

async function sendMessage() {
  const content = inputText.value.trim()
  if (!content) return
  closeMention()
  chatbiStore.sendMessage(content, collectedMentions.value)
  collectedMentions.value = []
  inputText.value = ''
}

function useExample(question: string) {
  inputText.value = question
  nextTick(() => panelRef.value?.textareaRef?.focus())
}

function closeMention() {
  mentionActive.value = false
  mentionQuery.value = ''
  mentionStartIdx.value = -1
  mentionSelectedIdx.value = 0
  mentionGroups.value = []
}

function _detectMention() {
  const ta = panelRef.value?.textareaRef
  if (!ta) return
  const cursor = ta.selectionStart
  const text = inputText.value

  let atIdx = -1
  for (let i = cursor - 1; i >= 0; i--) {
    const ch = text[i]
    if (ch === '@' && (i === 0 || /\s/.test(text[i - 1]))) {
      atIdx = i
      break
    }
    if (ch === ' ' || ch === '\n') break
  }

  if (atIdx === -1 || atIdx >= cursor) {
    closeMention()
    return
  }

  mentionStartIdx.value = atIdx
  mentionQuery.value = text.slice(atIdx + 1, cursor)
  mentionSelectedIdx.value = 0

  scheduleMentionSearch(mentionQuery.value)

  mentionActive.value = true
}

function selectMention(item: MentionItem) {
  if (mentionStartIdx.value < 0) return
  const text = inputText.value
  const before = text.slice(0, mentionStartIdx.value)
  const after = text.slice(mentionStartIdx.value + 1 + mentionQuery.value.length)
  inputText.value = `${before}@${item.label}${after}`
  collectedMentions.value.push(item)
  closeMention()

  nextTick(() => {
    const ta = panelRef.value?.textareaRef
    if (ta) {
      const pos = mentionStartIdx.value + 1 + item.label.length
      ta.focus()
      ta.setSelectionRange(pos, pos)
    }
  })
}

function confirmMention() {
  const items = flatMentionItems.value
  if (items.length === 0) return
  const idx = Math.min(mentionSelectedIdx.value, items.length - 1)
  selectMention(items[idx].item)
}

function handleKeydown(e: KeyboardEvent) {
  if (mentionActive.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionSelectedIdx.value = Math.min(mentionSelectedIdx.value + 1, flatMentionItems.value.length - 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      mentionSelectedIdx.value = Math.max(mentionSelectedIdx.value - 1, 0)
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      confirmMention()
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closeMention()
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

function onInput(e: Event) {
  const ta = e.target as HTMLTextAreaElement
  inputText.value = ta.value
  _detectMention()
}

function groupIcon(type: string): string {
  switch (type) {
    case 'table':
      return 'server'
    case 'knowledge':
      return 'book'
    case 'adhoc':
      return 'file-paste'
    default:
      return 'file'
  }
}
</script>

<template>
  <ChatTimelinePanel
    ref="panelRef"
    v-model:input-text="inputText"
    :store="chatbiStore"
    :title="t('chatbi.title')"
    session-label-namespace="chatbi.sessions"
    :placeholder="t('chatbi.placeholder')"
    :thinking-label="t('chatbi.thinking')"
    :send-label="t('chatbi.send')"
    :cancel-label="t('common.cancel')"
    @send="sendMessage"
    @input="onInput"
    @keydown="handleKeydown"
  >
    <template #welcome>
      <div class="welcome">
        <t-icon name="chart-bar" size="40px" class="welcome-icon" />
        <p class="welcome-title">{{ t('chatbi.welcome') }}</p>
        <p class="welcome-text">{{ t('chatbi.placeholder') }}</p>
        <div class="welcome-examples">
          <div
            v-for="(q, idx) in [t('chatbi.examples.q1'), t('chatbi.examples.q2'), t('chatbi.examples.q3')]"
            :key="idx"
            class="example-card"
            @click="useExample(q)"
          >
            <t-icon name="chat" size="14px" class="example-icon" />
            <span>{{ q }}</span>
          </div>
        </div>
      </div>
    </template>

    <template #mention-popup>
      <div v-if="mentionActive" class="mention-popup">
        <div v-if="mentionLoading" class="mention-loading">
          {{ t('chatbi.mention.loading') }}
        </div>
        <template v-for="entry in flatMentionItems" :key="entry.item.id">
          <div
            v-if="entry.item === entry.group.items[0]"
            class="mention-group-header"
          >
            <t-icon :name="groupIcon(entry.group.type)" size="12px" />
            <span>{{ t(`chatbi.mention.${entry.group.type}`) }}</span>
          </div>
          <div
            class="mention-item"
            :class="{ highlighted: entry.flatIdx === mentionSelectedIdx }"
            @mousedown.prevent="selectMention(entry.item)"
          >
            <t-icon :name="entry.item.icon" size="13px" class="mention-item-icon" />
            <span class="mention-item-label">{{ entry.item.label }}</span>
            <span class="mention-item-subtitle">{{ entry.item.subtitle }}</span>
          </div>
        </template>
        <div v-if="!mentionLoading && flatMentionItems.length === 0" class="mention-empty">
          {{ t('chatbi.mention.noResults') }}
        </div>
      </div>
    </template>

    <template #selectors-extra>
      <ExecutionStylePicker
        :model-value="chatbiStore.currentExecutionStyle"
        @update:model-value="chatbiStore.switchExecutionStyle"
      />
    </template>
  </ChatTimelinePanel>
</template>

<style scoped>
.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
  padding: 40px 20px;
}

.welcome-icon {
  opacity: 0.5;
  color: var(--td-brand-color);
}

.welcome-title {
  font-size: var(--td-font-size-title-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  margin: 0;
}

.welcome-text {
  font-size: var(--td-font-size-body-medium);
  color: var(--td-text-color-placeholder);
  text-align: center;
  max-width: 320px;
  margin: 0;
  line-height: 1.6;
}

.welcome-examples {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  width: 100%;
  max-width: 360px;
}

.example-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  background: var(--td-bg-color-secondarycontainer);
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-small);
  cursor: pointer;
}

.example-card:hover {
  border-color: var(--td-brand-color);
  background: var(--td-bg-color-container-hover);
}

.mention-popup {
  position: absolute;
  bottom: calc(100% - 4px);
  left: 14px;
  right: 14px;
  max-height: 280px;
  overflow-y: auto;
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  box-shadow: var(--td-shadow-2);
  z-index: 50;
}

.mention-loading,
.mention-empty {
  padding: 12px 16px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.mention-group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--td-text-color-secondary);
  text-transform: uppercase;
}

.mention-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  cursor: pointer;
  font-size: var(--td-font-size-body-small);
}

.mention-item:hover,
.mention-item.highlighted {
  background: var(--td-bg-color-container-hover);
}

.mention-item-subtitle {
  color: var(--td-text-color-secondary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 11px;
  flex: 1;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
