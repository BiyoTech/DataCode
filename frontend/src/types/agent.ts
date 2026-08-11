/**
 * ACP v2 Protocol TypeScript type definitions.
 * Aligns with backend protocol/messages.py event taxonomy.
 * All timestamps are UTC ISO 8601 strings.
 */

// ============================================================
// Session & Mode
// ============================================================

export type SessionMode = 'ask' | 'craft' | 'design'

// Execution style — autonomy dimension, orthogonal to SessionMode (capability).
// 'agent' = autonomous run-to-completion (default); 'interactive' = step-by-step.
export type ExecutionStyle = 'agent' | 'interactive'

export type ConnectionState = 'connected' | 'connecting' | 'reconnecting' | 'disconnected'

// ============================================================
// ACP v2 Event Types (Server → Client)
// ============================================================

export type EventType =
  // Agent lifecycle
  | 'agent.start'
  | 'agent.finish'
  | 'agent.delegate'
  // Thought / reasoning
  | 'thought.plan'
  | 'thought.update'
  // Tool execution
  | 'tool.start'
  | 'tool.progress'
  | 'tool.end'
  | 'tool.error'
  // Artifacts
  | 'artifact.created'
  | 'artifact.diff'
  | 'artifact.file'
  | 'artifact.image'
  | 'artifact.chart'
  // Todo / task list
  | 'todo.update'
  // Human-in-the-loop
  | 'human.required'
  | 'human.response'
  // Message streaming
  | 'message.delta'
  | 'message.complete'
  // System notifications
  | 'system.warning'
  | 'system.error'

// ============================================================
// ACP v2 Event Envelope (wire format)
// ============================================================

export interface ACPEvent {
  type: EventType
  session_id: string
  event_id: string
  parent_event_id?: string
  timestamp: string
  payload: Record<string, any>
}

// ============================================================
// Event Payload Interfaces
// ============================================================

export interface AgentStartPayload {
  agent_name: string
  mode: SessionMode
  task_id?: string
}

export interface AgentFinishPayload {
  agent_name: string
  outcome: 'success' | 'error' | 'done' | 'partial'
  summary?: string
}

export interface AgentDelegatePayload {
  from_agent: string
  to_agent: string
  reason?: string
}

export interface ThoughtPlanPayload {
  agent_name: string
  plan: string[]
  estimated_steps: number
}

export interface ThoughtUpdatePayload {
  agent_name: string
  thought: string
}

export interface ToolStartPayload {
  tool_name: string
  agent_name: string
  arguments: Record<string, any>
  task_id?: string
}

export interface ToolProgressPayload {
  tool_name: string
  message: string
  progress: number
}

export interface ToolEndPayload {
  tool_name: string
  result: any
  duration_ms?: number
}

export interface ToolErrorPayload {
  tool_name: string
  error: string
  code: string
  recoverable: boolean
}

export interface ArtifactCreatedPayload {
  artifact_id: string
  artifact_type: string
  file_path: string
  language: string
  content: string
  task_id?: string
}

export interface ArtifactDiffPayload {
  artifact_id: string
  file_path: string
  language: string
  diff: string
  // Cursor-style change metadata (see backend protocol/messages.py artifact_diff)
  change_type: 'create' | 'update' | 'delete'
  lines_added: number
  lines_removed: number
  // Full before/after content powering the side-by-side diff viewer
  old_content: string
  new_content: string
  task_id?: string
}

export interface TodoUpdatePayload {
  tasks: TodoItem[]
  total: number
  completed: number
  in_progress: number
}

export interface TodoItem {
  id: number
  content: string
  status: 'pending' | 'in_progress' | 'completed' | 'deleted'
  /** Optional present-continuous label, e.g. "正在执行 X". Shown as a
   *  secondary line under content when present. */
  activeForm?: string
}

export interface HumanRequiredPayload {
  request_id: string
  request_type: string
  title: string
  description?: string
  tool_name?: string
  tool_level?: number
  risk_badge_label?: string
  risk_badge_tone?: 'low' | 'medium' | 'high'
  options: ApprovalOption[]
  task_id?: string
  // Command-level approval extensions (shell command security policy)
  commands?: string[]
  rule_name?: string
  reason?: string
}

export interface ApprovalOption {
  id: string
  label: string
  style: 'primary' | 'secondary' | 'danger'
}

export interface MessageDeltaPayload {
  agent_name: string
  delta: string
  task_id?: string
}

export interface MessageCompletePayload {
  agent_name: string
  content: string
  task_id?: string
}

export interface SystemErrorPayload {
  code: string
  message: string
  recoverable: boolean
}

export interface SystemWarningPayload {
  code: string
  message: string
  suggestion?: string
}

// ============================================================
// Client → Server Message Types
// ============================================================

export type ClientMessageType =
  | 'chat.message'
  | 'chat.cancel'
  | 'action.confirm'
  | 'action.reject'
  | 'context.file_change'
  | 'context.file_open'

export interface ClientMessage {
  type: ClientMessageType
  session_id: string
  payload: Record<string, any>
}

// ============================================================
// Timeline Entry — UI data model for rendering
// ============================================================

export type TimelineEntryStatus = 'running' | 'success' | 'error' | 'warning' | 'pending' | 'cancelled'

export interface TimelineEntry {
  id: string
  type: EventType
  timestamp: string
  payload: Record<string, any>
  status?: TimelineEntryStatus
  collapsed?: boolean
  // Text currently displayed for a streaming message.delta bubble. The
  // typewriter pump reveals characters into this from `targetText`.
  accumulatedText?: string
  // Full text received so far for a streaming message.delta bubble. Decoupled
  // from `accumulatedText` so bursty/batched network delivery can still be
  // rendered as a smooth character-by-character stream.
  targetText?: string
}

// ============================================================
// Chat Session
// ============================================================

export interface ChatSession {
  session_id: string
  title: string
  mode: SessionMode
  execution_style?: ExecutionStyle
  status: string
  message_count: number
  created_at: string | null
  last_message_at: string | null
}
