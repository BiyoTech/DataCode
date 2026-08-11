<script setup lang="ts">
/**
 * ChatPanel — development-assistant conversation panel.
 */
import { ref, computed, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useChatStore } from '@/stores/chat'
import { useWorkspaceStore } from '@/stores/workspace'
import { useFileStore } from '@/stores/files'
import { useFileMention } from '@/composables/useFileMention'
import ChatTimelinePanel from '@/components/chat/ChatTimelinePanel.vue'
import ExecutionStylePicker from '@/components/chat/ExecutionStylePicker.vue'
import type { SessionMode } from '@/types/agent'

const { t } = useI18n()
const chatStore = useChatStore()
const workspaceStore = useWorkspaceStore()
const fileStore = useFileStore()
const { pendingMention, consumeMention } = useFileMention()

const inputText = ref('')
const panelRef = ref<InstanceType<typeof ChatTimelinePanel> | null>(null)
const showModeMenu = ref(false)

const mentionActive = ref(false)
const mentionQuery = ref('')
const mentionStartIdx = ref(-1)
const mentionSelectedIdx = ref(0)
const mentionFilesLoading = ref(false)

const mentionSuggestions = computed(() => {
  const q = mentionQuery.value.toLowerCase()
  const all = fileStore.flatFiles
  if (!q) return all.slice(0, 20)
  return all.filter(
    (f) => f.path.toLowerCase().includes(q) || f.name.toLowerCase().includes(q),
  ).slice(0, 20)
})

function closeModeMenu(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.mode-picker')) {
    showModeMenu.value = false
  }
}
onMounted(() => document.addEventListener('click', closeModeMenu))
onBeforeUnmount(() => document.removeEventListener('click', closeModeMenu))

const modes: { id: SessionMode; icon: string }[] = [
  { id: 'craft', icon: 'folder' },
  { id: 'ask', icon: 'chat' },
  { id: 'design', icon: 'edit' },
]

onMounted(async () => {
  chatStore.fetchModels()
  await chatStore.initSession()
})

function selectMode(mode: SessionMode) {
  chatStore.switchMode?.(mode)
  showModeMenu.value = false
}

// The dev-assistant store is created with enableModeSwitch: true, so these
// optional fields are always present at runtime. Pinia auto-unwraps refs on the
// store instance, so `chatStore.currentMode` is already the plain value.
const currentMode = computed<SessionMode>(() => chatStore.currentMode ?? 'craft')
const currentModeLabel = computed(() => t(`chat.modes.${currentMode.value}`))
const currentModeIcon = computed(() => modes.find((m) => m.id === currentMode.value)?.icon || 'chat')

async function sendMessage() {
  const content = inputText.value.trim()
  if (!content) return
  closeMention()
  inputText.value = ''
  chatStore.sendMessage(content)
}

function closeMention() {
  mentionActive.value = false
  mentionQuery.value = ''
  mentionStartIdx.value = -1
  mentionSelectedIdx.value = 0
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

  const ws = workspaceStore.currentWorkspace
  if (ws && fileStore.flatFiles.length === 0 && !mentionFilesLoading.value) {
    mentionFilesLoading.value = true
    fileStore.fetchFlatFiles(ws.path).finally(() => {
      mentionFilesLoading.value = false
    })
  }

  mentionActive.value = true
}

function selectMention(filePath: string) {
  if (mentionStartIdx.value < 0) return
  const text = inputText.value
  const before = text.slice(0, mentionStartIdx.value)
  const after = text.slice(mentionStartIdx.value + 1 + mentionQuery.value.length)
  inputText.value = `${before}@${filePath}${after}`
  closeMention()

  nextTick(() => {
    const ta = panelRef.value?.textareaRef
    if (ta) {
      const pos = mentionStartIdx.value + 1 + filePath.length
      ta.focus()
      ta.setSelectionRange(pos, pos)
    }
  })
}

function confirmMention() {
  const suggestions = mentionSuggestions.value
  if (suggestions.length === 0) return
  const idx = Math.min(mentionSelectedIdx.value, suggestions.length - 1)
  selectMention(suggestions[idx].path)
}

function handleKeydown(e: KeyboardEvent) {
  if (mentionActive.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionSelectedIdx.value = Math.min(mentionSelectedIdx.value + 1, mentionSuggestions.value.length - 1)
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

watch(pendingMention, (path) => {
  if (!path) return
  const consumedPath = consumeMention()
  if (!consumedPath) return

  const ta = panelRef.value?.textareaRef
  if (ta) {
    const cursor = ta.selectionStart
    const text = inputText.value
    const mentionToken = ` @${consumedPath} `
    inputText.value = text.slice(0, cursor) + mentionToken + text.slice(cursor)
    nextTick(() => {
      ta.focus()
      const newPos = cursor + mentionToken.length
      ta.setSelectionRange(newPos, newPos)
    })
  } else {
    inputText.value += ` @${consumedPath} `
  }
})
</script>

<template>
  <ChatTimelinePanel
    ref="panelRef"
    v-model:input-text="inputText"
    :store="chatStore"
    :title="t('nav.chat')"
    session-label-namespace="chat.sessions"
    :placeholder="t('chat.placeholder')"
    :thinking-label="t('chat.thinking.label')"
    :send-label="t('chat.send')"
    :cancel-label="t('common.cancel')"
    @send="sendMessage"
    @input="onInput"
    @keydown="handleKeydown"
  >
    <template #welcome>
      <div class="welcome">
        <t-icon name="logo-github" size="40px" class="welcome-icon" />
        <p class="welcome-title">{{ t('nav.chat') }}</p>
        <p class="welcome-text">{{ t('chat.placeholder') }}</p>
      </div>
    </template>

    <template #mention-popup>
      <div v-if="mentionActive" class="mention-popup">
        <div v-if="mentionFilesLoading" class="mention-loading">
          {{ t('chat.mention.loading') }}
        </div>
        <div
          v-for="(file, idx) in mentionSuggestions"
          :key="file.path"
          class="mention-item"
          :class="{ highlighted: idx === mentionSelectedIdx }"
          @mousedown.prevent="selectMention(file.path)"
        >
          <t-icon name="file" size="13px" class="mention-file-icon" />
          <span class="mention-file-name">{{ file.name }}</span>
          <span class="mention-file-path">{{ file.path }}</span>
        </div>
        <div v-if="!mentionFilesLoading && mentionSuggestions.length === 0" class="mention-empty">
          {{ t('chat.mention.noResults') }}
        </div>
      </div>
    </template>

    <template #selectors-extra>
      <ExecutionStylePicker
        :model-value="chatStore.currentExecutionStyle"
        @update:model-value="chatStore.switchExecutionStyle"
      />
      <div class="mode-picker" @click.stop>
        <button class="mode-btn" @click="showModeMenu = !showModeMenu">
          <t-icon :name="currentModeIcon" size="14px" class="mode-icon" />
          <span class="mode-label">{{ currentModeLabel }}</span>
          <t-icon name="chevron-down" size="10px" class="mode-chevron" />
        </button>
        <div v-if="showModeMenu" class="mode-menu">
          <div
            v-for="m in modes"
            :key="m.id"
            class="mode-menu-item"
            :class="{ active: currentMode === m.id }"
            @click="selectMode(m.id)"
          >
            <t-icon :name="m.icon" size="14px" class="mode-menu-icon" />
            <span class="mode-menu-label">{{ t(`chat.modes.${m.id}`) }}</span>
            <t-icon v-if="currentMode === m.id" name="check" size="13px" class="mode-check" />
          </div>
        </div>
      </div>
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

.welcome-icon { opacity: 0.5; }

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
  max-width: 300px;
  margin: 0;
  line-height: 1.6;
}

.mention-popup {
  position: absolute;
  bottom: calc(100% - 4px);
  left: 14px;
  right: 14px;
  max-height: 240px;
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

.mention-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  cursor: pointer;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-primary);
  transition: background 0.08s;
}

.mention-item:hover,
.mention-item.highlighted {
  background: var(--td-bg-color-container-hover);
}

.mention-file-icon {
  flex-shrink: 0;
  color: var(--td-text-color-secondary);
}

.mention-file-name {
  font-weight: 500;
  white-space: nowrap;
}

.mention-file-path {
  color: var(--td-text-color-secondary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  text-align: right;
}

.mode-picker { position: relative; }

.mode-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-small);
  padding: 4px 8px;
  cursor: pointer;
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-small);
  font-weight: 500;
}

.mode-menu {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  min-width: 140px;
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 4px;
  box-shadow: var(--td-shadow-3);
  z-index: 100;
}

.mode-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: var(--td-radius-small);
  cursor: pointer;
  font-size: var(--td-font-size-body-medium);
}

.mode-menu-item:hover { background: var(--td-bg-color-container-hover); }
.mode-menu-item.active { color: var(--td-brand-color); }
.mode-menu-label { flex: 1; }
.mode-check { color: var(--td-brand-color); }
</style>
