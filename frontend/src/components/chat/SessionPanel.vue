<script setup lang="ts">
/**
 * SessionPanel — slide-over panel showing session history list.
 * Supports creating new sessions, switching between sessions, and deleting sessions.
 *
 * Decoupled from any specific store: the parent component passes a store
 * instance via the `store` prop so the same panel can serve both the
 * development assistant (ChatPanel) and the ChatBI analyst (AbiAgentChat).
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatRelativeTime } from '@/utils/timezone'
import type { ChatSession } from '@/types/agent'

/**
 * Minimal store interface that SessionPanel depends on.
 * Both useChatStore and useChatbiStore satisfy this contract.
 */
export interface SessionStoreLike {
  sessions: ChatSession[]
  currentSessionId: string | null
  sessionPanelOpen: boolean
  createNewSession: () => Promise<void>
  switchSession: (id: string) => Promise<void>
  deleteSession: (id: string) => Promise<void>
  toggleSessionPanel: () => void
}

const props = defineProps<{
  store: SessionStoreLike
  /** i18n namespace for session labels, e.g. 'chat.sessions' or 'chatbi.sessions'. */
  labelNamespace?: string
}>()

const { t, te } = useI18n()

const ns = computed(() => props.labelNamespace || 'chat.sessions')

/** Resolve a translation key, falling back to the default chat namespace. */
function tx(key: string, fallback: string): string {
  const fullKey = `${ns.value}.${key}`
  return te(fullKey) ? t(fullKey) : t(fallback)
}

const sessionList = computed(() => props.store.sessions)

function onNewChat() {
  props.store.createNewSession()
  props.store.toggleSessionPanel()
}

function onSelectSession(sessionId: string) {
  props.store.switchSession(sessionId)
}

function onDeleteSession(sessionId: string, event: Event) {
  event.stopPropagation()
  props.store.deleteSession(sessionId)
}

function onClose() {
  props.store.toggleSessionPanel()
}

/**
 * Format a UTC timestamp for session display. Delegates to the shared timezone
 * util so relative-time rendering stays consistent (backend stores UTC; the
 * frontend converts to the user's locale).
 */
function formatTime(isoStr: string | null): string {
  if (!isoStr) return ''
  return formatRelativeTime(isoStr)
}
</script>

<template>
  <div class="session-panel-overlay" @click.self="onClose">
    <div class="session-panel">
      <!-- Header -->
      <div class="panel-header">
        <span class="panel-title">{{ tx('title', 'chat.sessions.title') }}</span>
        <button class="close-btn" @click="onClose" :title="t('common.cancel')"><t-icon name="close" size="14px" /></button>
      </div>

      <!-- New chat button -->
      <button class="new-chat-btn" @click="onNewChat">
        <span class="new-chat-icon">＋</span>
        <span>{{ tx('newChat', 'chat.sessions.newChat') }}</span>
      </button>

      <!-- Session list -->
      <div class="session-list">
        <div v-if="sessionList.length === 0" class="empty-state">
          {{ tx('empty', 'chat.sessions.empty') }}
        </div>
        <div
          v-for="session in sessionList"
          :key="session.session_id"
          class="session-item"
          :class="{ active: session.session_id === props.store.currentSessionId }"
          @click="onSelectSession(session.session_id)"
        >
          <div class="session-info">
            <span class="session-title">{{ session.title || tx('untitled', 'chat.sessions.untitled') }}</span>
            <span class="session-meta">
              {{ session.message_count }} {{ tx('messages', 'chat.sessions.messages') }}
              · {{ formatTime(session.last_message_at || session.created_at) }}
            </span>
          </div>
          <button
            class="delete-btn"
            @click="onDeleteSession(session.session_id, $event)"
            :title="t('common.delete')"
          >
            🗑
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.session-panel-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0,0, 0.3);
  z-index: 200;
  display: flex;
  justify-content: flex-end;
}

.session-panel {
  width: 300px;
  max-width: 90%;
  height: 100%;
  background: var(--bg-primary);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 16px rgba(0, 0, 0, 0.2);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.close-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
  border-radius: 4px;
}

.close-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.new-chat-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 10px 14px;
  padding: 8px 12px;
  background: var(--accent);
  color: var(--accent-foreground);
  border: none;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.15s;
}

.new-chat-btn:hover {
  opacity: 0.9;
}

.new-chat-icon {
  font-size: 14px;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px;
}

.empty-state {
  padding: 24px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
}

.session-item {
  display: flex;
  align-items: center;
  padding: 10px 10px;
  margin: 2px 0;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s;
}

.session-item:hover {
  background: var(--bg-hover);
}

.session-item.active {
  background: var(--bg-hover);
  border-left: 2px solid var(--accent);
  padding-left: 8px;
}

.session-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.session-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  font-size: 11px;
  color: var(--text-muted);
}

.delete-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 0.12s;
}

.session-item:hover .delete-btn {
  opacity: 0.6;
}

.delete-btn:hover {
  opacity: 1 !important;
  background: var(--bg-hover);
}
</style>
