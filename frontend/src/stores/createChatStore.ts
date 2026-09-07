/**
 * Shared chat session store factory used by dev-assistant and ChatBI stores.
 */
import { defineStore } from 'pinia'
import { ref, computed, type Ref, type ComputedRef } from 'vue'
import api from '@/utils/api'
import { extractApiMessage, notifyError } from '@/utils/notifyError'
import { i18n } from '@/i18n'
import { useSSE } from '@/composables/useSSE'
import { useWorkspaceStore } from '@/stores/workspace'
import { useAuthStore } from '@/stores/auth'
import { useEnvironmentStore } from '@/stores/environment'
import type {
  ACPEvent,
  TimelineEntry,
  SessionMode,
  ExecutionStyle,
  ConnectionState,
  ChatSession,
} from '@/types/agent'

export interface ChatMentionPayload {
  type: string
  id: string
  label: string
  token: string
}

/**
 * Strongly-typed contract every created chat store satisfies.
 *
 * `currentMode` / `switchMode` / `lastAssistantText` are present only when the
 * store was created with `enableModeSwitch: true`; consumers should feature-
 * detect before using them.
 */
export interface ChatStoreInstance {
  // State
  sessions: Ref<ChatSession[]>
  currentSessionId: Ref<string | null>
  timeline: Ref<TimelineEntry[]>
  isThinking: Ref<boolean>
  hasPendingApproval: ComputedRef<boolean>
  connectionState: Ref<ConnectionState>
  sessionPanelOpen: Ref<boolean>
  availableModels: Ref<{ id: string; name: string; provider: string }[]>
  selectedModelId: Ref<string>
  isConnected: ComputedRef<boolean>
  currentExecutionStyle: Ref<ExecutionStyle>
  // Lifecycle
  initSession: () => Promise<void>
  createNewSession: () => Promise<string | undefined>
  loadSession: (sessionId: string) => Promise<boolean>
  switchSession: (sessionId: string) => Promise<void>
  deleteSession: (sessionId: string) => Promise<void>
  fetchSessions: () => Promise<void>
  toggleSessionPanel: () => void
  fetchModels: () => Promise<void>
  setModel: (modelId: string) => void
  sendMessage: (content: string, mentions?: ChatMentionPayload[]) => Promise<void>
  sendApproval: (requestId: string, approved: boolean, reason?: string) => Promise<void>
  cancelChat: () => Promise<void>
  disconnectSession: () => void
  pushError: (message: string, detail?: string) => void
  switchExecutionStyle: (style: ExecutionStyle) => Promise<void>
  // Conditionally present when enableModeSwitch is true
  currentMode?: Ref<SessionMode>
  switchMode?: (mode: SessionMode) => Promise<void>
  lastAssistantText?: ComputedRef<string>
}

export interface ChatStoreConfig {
  storeId: string
  lsSessionKey: string
  scenario?: string
  defaultMode: SessionMode
  defaultExecutionStyle?: ExecutionStyle
  enableTypewriter: boolean
  enableModeSwitch: boolean
}

export function createChatStore(config: ChatStoreConfig) {
  return defineStore(config.storeId, () => {
    const sessions = ref<ChatSession[]>([])
    const currentSessionId = ref<string | null>(null)
    const currentMode = ref<SessionMode>(config.defaultMode)
    const currentExecutionStyle = ref<ExecutionStyle>(config.defaultExecutionStyle || 'agent')
    const timeline = ref<TimelineEntry[]>([])
    const isThinking = ref(false)
    // The run is paused waiting for a human approval decision when any
    // human.required entry is still pending. This is distinct from "completed":
    // the agent is blocked, not finished, so the UI must not present as idle.
    const hasPendingApproval = computed(() =>
      timeline.value.some((e) => e.type === 'human.required' && e.status === 'pending'),
    )
    const connectionState = ref<ConnectionState>('disconnected')
    const sessionPanelOpen = ref(false)

    const availableModels = ref<{ id: string; name: string; provider: string }[]>([])
    const selectedModelId = ref<string>('')

    // Cap the processed-event dedup set so a long-lived session never grows it
    // without bound. When exceeded we drop the oldest ids (Set preserves
    // insertion order), trading perfect dedup of very old events for memory.
    const processedEventIds = new Set<string>()
    const MAX_PROCESSED_EVENT_IDS = 5000

    function rememberProcessedEvent(eventId: string) {
      processedEventIds.add(eventId)
      if (processedEventIds.size > MAX_PROCESSED_EVENT_IDS) {
        const overflow = processedEventIds.size - MAX_PROCESSED_EVENT_IDS
        const it = processedEventIds.values()
        for (let i = 0; i < overflow; i++) {
          const oldest = it.next().value
          if (oldest !== undefined) processedEventIds.delete(oldest)
        }
      }
    }

    // Map an agent.finish outcome to a timeline entry status. "paused" (e.g.
    // max-turns reached) surfaces as a warning; "cancelled" uses its own status
    // so the UI never shows a green "success" mark for an interrupted run.
    function mapFinishOutcome(outcome: unknown): TimelineEntry['status'] {
      switch (outcome) {
        case 'error':
          return 'error'
        case 'partial':
        case 'paused':
          return 'warning'
        case 'cancelled':
          return 'cancelled'
        default:
          return 'success'
      }
    }

    // True once the stream has been connected at least once, so a later
    // 'connected' is recognised as a RECONNECT rather than the initial attach.
    let streamWasConnected = false

    const { connect, disconnect } = useSSE({
      onEvent: handleServerEvent,
      onStateChange: (state) => {
        connectionState.value = state
        if (state === 'connected') {
          // Events published while the stream was down are only replayed when
          // the server still holds the session's buffer queue — a backend
          // restart loses it. Refill from the persisted timeline so a run that
          // completed during the outage is shown instead of silently missing.
          if (streamWasConnected) {
            void _refillTimeline()
          }
          streamWasConnected = true
        }
        // A terminal disconnect (reconnect attempts exhausted) while a run is
        // still in flight would otherwise leave the UI stuck in the "thinking"
        // state forever. Surface a warning and release the busy state so the
        // composer returns to an idle, actionable look.
        if (state === 'disconnected' && isThinking.value) {
          isThinking.value = false
          pushWarning('CONNECTION_LOST', 'Connection lost. The run may still be running on the server.')
        }
      },
    })

    /**
     * Attach the event stream to a session. Starting a session is not a
     * reconnect, so the refill-on-reconnect flag restarts with it.
     */
    function _startStream(sessionId: string) {
      streamWasConnected = false
      connect(sessionId)
    }

    /**
     * Rebuild the timeline from the server's persisted events.
     *
     * Used after a reconnect: the persisted timeline is authoritative, so this
     * both fills gaps and drops any half-streamed bubble whose remaining
     * deltas were lost with the connection.
     */
    async function _refillTimeline() {
      const sessionId = currentSessionId.value
      if (!sessionId) return
      try {
        const { data } = await api.get(`/sessions/${sessionId}`)
        const events = data.timeline_events || []
        if (events.length === 0) return
        timeline.value = _buildTimelineFromEvents(events)
        processedEventIds.clear()
        for (const ev of events) {
          if (ev.event_id) processedEventIds.add(ev.event_id)
        }
      } catch {
        // Keep whatever the client already has: a failed refill must not wipe
        // the visible conversation.
      }
    }

    const isConnected = computed(() => connectionState.value === 'connected')

    const lastAssistantText = computed(() => {
      for (let i = timeline.value.length - 1; i >= 0; i--) {
        const entry = timeline.value[i]
        if (entry.type === 'message.complete') {
          return entry.payload.content || ''
        }
      }
      return ''
    })

    function pushError(message: string, detail?: string) {
      const entry: TimelineEntry = {
        id: crypto.randomUUID(),
        type: 'system.error',
        timestamp: new Date().toISOString(),
        payload: { message, detail: detail || '' },
        status: 'error',
      }
      timeline.value.push(entry)
      isThinking.value = false
    }

    function pushWarning(code: string, message: string, suggestion?: string) {
      const entry: TimelineEntry = {
        id: crypto.randomUUID(),
        type: 'system.warning',
        timestamp: new Date().toISOString(),
        payload: { code, message, suggestion: suggestion || '' },
        status: 'warning',
      }
      timeline.value.push(entry)
    }

    async function initSession() {
      const savedId = localStorage.getItem(config.lsSessionKey)
      if (savedId) {
        const loaded = await loadSession(savedId)
        if (loaded) return
      }
      await createNewSession()
    }

    async function createNewSession() {
      const authStore = useAuthStore()
      // The gateway re-derives the owner from the access token.
      const userId = authStore.user?.userId ?? ''

      try {
        disconnect()

        const body: Record<string, unknown> = {
          user_id: userId,
          mode: config.enableModeSwitch ? currentMode.value : config.defaultMode,
          execution_style: currentExecutionStyle.value,
        }
        if (config.scenario) {
          body.scenario = config.scenario
        }

        const { data } = await api.post('/sessions', body)

        currentSessionId.value = data.session_id
        if (config.enableModeSwitch) {
          currentMode.value = data.mode || config.defaultMode
        }
        currentExecutionStyle.value =
          data.execution_style || config.defaultExecutionStyle || 'agent'
        timeline.value = []
        processedEventIds.clear()

        localStorage.setItem(config.lsSessionKey, data.session_id)
        _startStream(data.session_id)
        fetchSessions()

        return data.session_id
      } catch (e: any) {
        // Single parsing path: it localizes authorization denials and surfaces
        // the backend root cause instead of "status code 4xx".
        const prefix = i18n.global.t('chat.error.sessionFailed')
        pushError(`${prefix}: ${extractApiMessage(e)}`)
      }
    }

    async function loadSession(sessionId: string): Promise<boolean> {
      try {
        disconnect()

        const { data } = await api.get(`/sessions/${sessionId}`)

        currentSessionId.value = data.session_id
        if (config.enableModeSwitch) {
          currentMode.value = data.mode || config.defaultMode
        }
        currentExecutionStyle.value =
          data.execution_style || config.defaultExecutionStyle || 'agent'

        const timelineEvents: Array<{
          event_id: string
          type: string
          timestamp: string
          payload: Record<string, any>
          parent_event_id?: string
        }> = data.timeline_events || []
        const messages: Array<{ id: string; role: string; content: string; timestamp: string }> =
          data.messages || []

        if (timelineEvents.length > 0) {
          timeline.value = _buildTimelineFromEvents(timelineEvents)
        } else {
          timeline.value = messages.map((msg) => ({
            id: msg.id || crypto.randomUUID(),
            type: 'message.complete' as const,
            timestamp: msg.timestamp || new Date().toISOString(),
            payload: {
              role: msg.role,
              content: msg.content,
              agent_name: msg.role === 'assistant' ? 'agent' : '',
            },
            status: 'success' as const,
          }))
        }

        processedEventIds.clear()
        for (const ev of timelineEvents) {
          if (ev.event_id) processedEventIds.add(ev.event_id)
        }

        localStorage.setItem(config.lsSessionKey, data.session_id)
        _startStream(data.session_id)

        return true
      } catch {
        localStorage.removeItem(config.lsSessionKey)
        return false
      }
    }

    async function switchSession(sessionId: string) {
      if (sessionId === currentSessionId.value) {
        sessionPanelOpen.value = false
        return
      }
      await loadSession(sessionId)
      sessionPanelOpen.value = false
    }

    async function deleteSession(sessionId: string) {
      try {
        await api.delete(`/sessions/${sessionId}`)
        sessions.value = sessions.value.filter((s) => s.session_id !== sessionId)

        if (sessionId === currentSessionId.value) {
          localStorage.removeItem(config.lsSessionKey)
          await createNewSession()
        }
      } catch (e: any) {
        notifyError(e, 'Failed to delete session')
      }
    }

    async function fetchSessions() {
      const authStore = useAuthStore()
      // The gateway re-derives the owner from the access token.
      const userId = authStore.user?.userId ?? ''

      try {
        const params: Record<string, string> = { user_id: userId }
        if (config.scenario) {
          params.scenario = config.scenario
        }
        const { data } = await api.get('/sessions', { params })
        sessions.value = data.sessions || []
      } catch (e: any) {
        notifyError(e, 'Failed to fetch sessions')
      }
    }

    function toggleSessionPanel() {
      sessionPanelOpen.value = !sessionPanelOpen.value
      if (sessionPanelOpen.value) {
        fetchSessions()
      }
    }

    async function switchMode(mode: SessionMode) {
      if (!config.enableModeSwitch) return
      currentMode.value = mode
      if (currentSessionId.value) {
        await api.post(`/sessions/${currentSessionId.value}/mode`, { mode })
      }
    }

    async function switchExecutionStyle(style: ExecutionStyle) {
      currentExecutionStyle.value = style
      if (currentSessionId.value) {
        try {
          await api.post(`/sessions/${currentSessionId.value}/execution-style`, {
            execution_style: style,
          })
        } catch (e: any) {
          notifyError(e, 'Failed to switch execution style')
        }
      }
    }

    async function fetchModels() {
      try {
        const { data } = await api.get('/models')
        availableModels.value = data.models || []
        if (!selectedModelId.value && data.default_model) {
          selectedModelId.value = data.default_model
        }
      } catch (e) {
        console.warn('Failed to fetch models:', e)
      }
    }

    function setModel(modelId: string) {
      selectedModelId.value = modelId
    }

    async function sendMessage(content: string, mentions: ChatMentionPayload[] = []) {
      if (!currentSessionId.value) {
        pushError('No active session. Please wait for session initialization.')
        return
      }

      const userEntry: TimelineEntry = {
        id: crypto.randomUUID(),
        type: 'message.complete',
        timestamp: new Date().toISOString(),
        payload: { role: 'user', content, agent_name: '' },
      }
      timeline.value.push(userEntry)
      isThinking.value = true

      try {
        const envStore = useEnvironmentStore()
        const workspaceStore = useWorkspaceStore()
        const body: Record<string, unknown> = {
          content,
          workspace_path: workspaceStore.currentWorkspace?.path || '',
          model_id: selectedModelId.value,
          environment: envStore.currentEnvironment,
          execution_style: currentExecutionStyle.value,
        }
        if (config.enableModeSwitch) {
          body.mode = currentMode.value
        }
        if (mentions.length > 0) {
          body.mentions = mentions.map((m) => ({
            type: m.type,
            id: m.id,
            label: m.label,
            token: m.token,
          }))
        }

        await api.post(`/sessions/${currentSessionId.value}/chat`, body)
      } catch (e: any) {
        // Roll back the just-added user bubble and busy state so the composer
        // is immediately usable again after a rejected send. Locate it by
        // identity: a second send fired before this one failed would otherwise
        // leave a ghost bubble for a message the backend never accepted.
        const idx = timeline.value.indexOf(userEntry)
        if (idx >= 0) {
          timeline.value.splice(idx, 1)
        }
        isThinking.value = false

        // 409: a run is already in flight for this session. Surface a soft,
        // non-blocking warning instead of a hard error entry.
        if (e?.response?.status === 409) {
          notifyError(e, 'A task is already running in this session. Please wait for it to finish.')
          return
        }
        pushError(extractApiMessage(e, 'Failed to send message'))
      }
    }

    async function sendApproval(requestId: string, approved: boolean, reason?: string) {
      if (!currentSessionId.value) return

      try {
        await api.post(`/sessions/${currentSessionId.value}/approve`, {
          request_id: requestId,
          approved,
          reason: reason || '',
        })
      } catch (e: any) {
        notifyError(e, 'Failed to send approval')
      }

      // Optimistically resolve the card; the backend also emits a human.response
      // event which reconciles reloaded sessions to the same state.
      _resolveApproval(requestId, approved)
    }

    /**
     * Mark the human.required card matching `requestId` as resolved. `approved`
     * true -> success, false -> cancelled. No-op when the card is absent.
     */
    function _resolveApproval(requestId: string | undefined, approved: unknown) {
      if (!requestId) return
      const entry = timeline.value.find(
        (e) => e.type === 'human.required' && e.payload.request_id === requestId,
      )
      if (entry) {
        entry.status = approved ? 'success' : 'cancelled'
      }
    }

    async function cancelChat() {
      if (!currentSessionId.value) return
      try {
        await api.post(`/sessions/${currentSessionId.value}/cancel`)
      } catch (e: any) {
        notifyError(e, 'Failed to cancel chat')
      }
    }

    function handleServerEvent(event: ACPEvent) {
      if (event.event_id) {
        if (processedEventIds.has(event.event_id)) {
          return
        }
        rememberProcessedEvent(event.event_id)
      }

      const entry: TimelineEntry = {
        id: event.event_id,
        type: event.type,
        timestamp: event.timestamp,
        payload: event.payload,
      }

      switch (event.type) {
        case 'agent.start':
          entry.status = 'running'
          timeline.value.push(entry)
          isThinking.value = true
          break

        case 'agent.finish':
          _flushStreamingBubble()
          entry.status = mapFinishOutcome(event.payload.outcome)
          timeline.value.push(entry)
          isThinking.value = false
          break

        case 'agent.delegate':
          entry.status = 'success'
          timeline.value.push(entry)
          break

        case 'thought.plan':
        case 'thought.update':
          timeline.value.push(entry)
          break

        case 'tool.start':
          entry.status = 'running'
          entry.collapsed = true
          timeline.value.push(entry)
          break

        case 'tool.end':
          _finalizeToolCall(event.payload.tool_name, 'success', event.payload.result)
          break

        case 'tool.error':
          _finalizeToolCall(event.payload.tool_name, 'error', event.payload.error)
          break

        case 'tool.progress':
          _updateToolProgress(event.payload.tool_name, event.payload.message, event.payload.progress)
          break

        case 'message.delta':
          _appendMessageDelta(event.payload.delta, event.payload.agent_name)
          break

        case 'message.complete':
          _finalizeMessage(event.payload.content, event.payload.agent_name, event.event_id)
          isThinking.value = false
          break

        case 'artifact.created':
        case 'artifact.diff':
          timeline.value.push(entry)
          break

        case 'todo.update':
          _upsertTodoEntry(entry)
          break

        case 'human.required': {
          const reqId = entry.payload?.request_id
          const existing = reqId
            ? timeline.value.find(
                (e) => e.type === 'human.required' && e.payload?.request_id === reqId,
              )
            : undefined
          if (existing) {
            existing.payload = entry.payload
          } else {
            entry.status = 'pending'
            timeline.value.push(entry)
          }
          isThinking.value = false
          break
        }

        case 'human.response': {
          // Resolve the matching pending approval card so a reloaded session and
          // a live session converge on the same state. Do not push a visible
          // entry — the resolution is reflected on the existing card.
          _resolveApproval(entry.payload?.request_id, entry.payload?.approved)
          break
        }

        case 'system.error':
          entry.status = 'error'
          timeline.value.push(entry)
          isThinking.value = false
          break

        case 'system.warning':
          timeline.value.push(entry)
          break

        default:
          timeline.value.push(entry)
      }
    }

    function _buildTimelineFromEvents(
      events: Array<{
        event_id: string
        type: string
        timestamp: string
        payload: Record<string, any>
        parent_event_id?: string
      }>,
    ): TimelineEntry[] {
      const entries: TimelineEntry[] = []

      for (const event of events) {
        if (event.type === 'tool.end') {
          const toolName = event.payload?.tool_name
          if (toolName) {
            for (let i = entries.length - 1; i >= 0; i--) {
              if (
                entries[i].type === 'tool.start' &&
                entries[i].payload?.tool_name === toolName
              ) {
                entries[i].status = 'success'
                entries[i].payload = { ...entries[i].payload, result: event.payload?.result }
                break
              }
            }
          }
          continue
        }

        // human.response carries the approval resolution but is not itself a
        // visible entry. Reconcile the matching human.required card in place so
        // a reloaded session mirrors the live view.
        if (event.type === 'human.response') {
          const reqId = event.payload?.request_id
          if (reqId) {
            for (let i = entries.length - 1; i >= 0; i--) {
              if (entries[i].type === 'human.required' && entries[i].payload?.request_id === reqId) {
                entries[i].status = event.payload?.approved ? 'success' : 'cancelled'
                break
              }
            }
          }
          continue
        }

        const entry: TimelineEntry = {
          id: event.event_id || crypto.randomUUID(),
          type: event.type as TimelineEntry['type'],
          timestamp: event.timestamp || new Date().toISOString(),
          payload: event.payload || {},
        }

        switch (event.type) {
          case 'agent.start':
          case 'agent.delegate':
          case 'tool.start':
          case 'message.complete':
          case 'artifact.created':
          case 'artifact.diff':
          case 'thought.plan':
          case 'thought.update':
            entry.status = 'success'
            break

          // Default a persisted approval to pending; a later human.response
          // event (handled above) flips it to success/cancelled. This avoids
          // falsely showing an unresolved approval as already executed.
          case 'human.required':
            entry.status = 'pending'
            break

          case 'agent.finish':
            entry.status = mapFinishOutcome(event.payload?.outcome)
            break

          case 'system.error':
            entry.status = 'error'
            break

          default:
            break
        }

        if (event.type === 'tool.start') {
          entry.collapsed = true
        }

        // Fold duplicate assistant paragraphs persisted across continuation
        // segments (mirrors the live _finalizeMessage dedup) so a reloaded
        // session matches the live view.
        if (event.type === 'message.complete') {
          const content = entry.payload?.content || ''
          if (content && entry.payload?.role !== 'user') {
            let dup = false
            for (let i = entries.length - 1; i >= 0; i--) {
              if (entries[i].type === 'message.complete') {
                dup =
                  entries[i].payload?.role !== 'user' &&
                  entries[i].payload?.agent_name === entry.payload?.agent_name &&
                  (entries[i].payload?.content || '') === content
                break
              }
            }
            if (dup) continue
          }
        }

        entries.push(entry)
      }

      return entries
    }

    function _upsertTodoEntry(entry: TimelineEntry) {
      const idx = timeline.value.findIndex((e) => e.type === 'todo.update')
      if (idx >= 0) {
        timeline.value[idx] = entry
      } else {
        timeline.value.push(entry)
      }
    }

    let typewriterRaf: number | null = null

    function _pumpTypewriter() {
      typewriterRaf = null
      const last = timeline.value[timeline.value.length - 1]
      if (!last || last.type !== 'message.delta' || last.status !== 'running') {
        return
      }

      const target = last.targetText || ''
      const shown = last.accumulatedText || ''
      if (shown.length < target.length) {
        const backlog = target.length - shown.length
        const step = Math.max(2, Math.ceil(backlog / 6))
        last.accumulatedText = target.slice(0, shown.length + step)
      }

      if ((last.accumulatedText || '').length < target.length) {
        typewriterRaf = requestAnimationFrame(_pumpTypewriter)
      }
    }

    function _ensureTypewriter() {
      if (typewriterRaf == null) {
        typewriterRaf = requestAnimationFrame(_pumpTypewriter)
      }
    }

    function _stopTypewriter() {
      if (typewriterRaf != null) {
        cancelAnimationFrame(typewriterRaf)
        typewriterRaf = null
      }
    }

    function _flushStreamingBubble() {
      if (config.enableTypewriter) {
        _stopTypewriter()
      }
      const last = timeline.value[timeline.value.length - 1]
      if (last && last.type === 'message.delta' && last.status === 'running') {
        last.accumulatedText = last.targetText || last.accumulatedText || ''
      }
    }

    function _appendMessageDelta(delta: string, agentName: string) {
      const last = timeline.value[timeline.value.length - 1]
      if (
        last &&
        last.type === 'message.delta' &&
        last.status === 'running' &&
        last.payload.agent_name === agentName
      ) {
        if (config.enableTypewriter) {
          last.targetText = (last.targetText || '') + delta
          _ensureTypewriter()
        } else {
          last.accumulatedText = (last.accumulatedText || '') + delta
        }
      } else {
        timeline.value.push({
          id: crypto.randomUUID(),
          type: 'message.delta',
          timestamp: new Date().toISOString(),
          payload: { agent_name: agentName, delta },
          accumulatedText: config.enableTypewriter ? '' : delta,
          targetText: config.enableTypewriter ? delta : undefined,
          status: 'running',
        })
        if (config.enableTypewriter) {
          _ensureTypewriter()
        }
      }
    }

    /**
     * Guard against duplicate assistant paragraphs. The backend may emit the
     * same finalized text twice across autonomous continuation segments (the
     * model restating its previous plan). Fold a new message.complete when the
     * most recent completed assistant message has identical content.
     */
    function _isDuplicateAssistantMessage(content: string, agentName: string): boolean {
      if (!content) return false
      for (let i = timeline.value.length - 1; i >= 0; i--) {
        const e = timeline.value[i]
        if (e.type === 'message.complete') {
          return (
            e.payload?.role !== 'user' &&
            e.payload?.agent_name === agentName &&
            (e.payload?.content || '') === content
          )
        }
      }
      return false
    }

    function _finalizeMessage(content: string, agentName: string, eventId: string) {
      if (config.enableTypewriter) {
        _stopTypewriter()
        timeline.value = timeline.value.filter(
          (e) => !(e.type === 'message.delta' && e.status === 'running'),
        )
        if (timeline.value.some((e) => e.type === 'message.complete' && e.id === eventId)) {
          return
        }
        if (_isDuplicateAssistantMessage(content, agentName)) {
          return
        }
        timeline.value.push({
          id: eventId,
          type: 'message.complete',
          timestamp: new Date().toISOString(),
          payload: { agent_name: agentName, content, role: 'assistant' },
          status: 'success',
        })
        return
      }

      const lastIdx = timeline.value.length - 1
      const last = timeline.value[lastIdx]
      if (last && last.type === 'message.delta' && last.payload.agent_name === agentName) {
        // Drop the trailing running delta bubble; replace it with the completed
        // message unless it is a duplicate of the previous assistant message.
        if (_isDuplicateAssistantMessage(content, agentName)) {
          timeline.value.splice(lastIdx, 1)
          return
        }
        timeline.value[lastIdx] = {
          id: eventId,
          type: 'message.complete',
          timestamp: new Date().toISOString(),
          payload: { agent_name: agentName, content, role: 'assistant' },
          status: 'success',
        }
      } else {
        if (_isDuplicateAssistantMessage(content, agentName)) {
          return
        }
        timeline.value.push({
          id: eventId,
          type: 'message.complete',
          timestamp: new Date().toISOString(),
          payload: { agent_name: agentName, content, role: 'assistant' },
          status: 'success',
        })
      }
    }

    function _finalizeToolCall(
      toolName: string,
      status: TimelineEntry['status'],
      result: any,
    ) {
      for (let i = timeline.value.length - 1; i >= 0; i--) {
        const entry = timeline.value[i]
        if (entry.type === 'tool.start' && entry.payload.tool_name === toolName && entry.status === 'running') {
          entry.status = status
          entry.payload = { ...entry.payload, result }
          return
        }
      }
    }

    function _updateToolProgress(toolName: string, message: string, progress: number) {
      for (let i = timeline.value.length - 1; i >= 0; i--) {
        const entry = timeline.value[i]
        if (entry.type === 'tool.start' && entry.payload.tool_name === toolName && entry.status === 'running') {
          entry.payload = { ...entry.payload, progress_message: message, progress }
          return
        }
      }
    }

    function disconnectSession() {
      if (config.enableTypewriter) {
        _stopTypewriter()
      }
      disconnect()
      currentSessionId.value = null
      timeline.value = []
      processedEventIds.clear()
    }

    const storeApi: ChatStoreInstance = {
      sessions,
      currentSessionId,
      timeline,
      isThinking,
      hasPendingApproval,
      connectionState,
      availableModels,
      selectedModelId,
      sessionPanelOpen,
      isConnected,
      initSession,
      createNewSession,
      loadSession,
      switchSession,
      deleteSession,
      fetchSessions,
      toggleSessionPanel,
      fetchModels,
      setModel,
      sendMessage,
      sendApproval,
      cancelChat,
      disconnectSession,
      pushError,
      currentExecutionStyle,
      switchExecutionStyle,
    }

    if (config.enableModeSwitch) {
      storeApi.currentMode = currentMode
      storeApi.switchMode = switchMode
      storeApi.lastAssistantText = lastAssistantText
    }

    return storeApi
  })
}
