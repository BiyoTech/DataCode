/**
 * Tool-output parsing helpers.
 *
 * Centralizes the logic that turns raw tool results into structured data the
 * timeline cards can render nicely:
 *  - stripAnsi: remove terminal escape / control sequences so shell output
 *    reads like a clean terminal instead of raw "\u001b[...m" noise.
 *  - parseShellResults: normalize the `shell` tool result into typed records.
 *  - parsePatch / parseCreateFile: turn an apply_patch / create_file payload
 *    into per-file diff blocks for an editor-style diff view.
 */

// ─────────────────────────── ANSI handling ───────────────────────────

// Well-known ANSI escape-sequence matcher (colors, cursor moves, bracketed
// paste mode like "\u001b[?2004l", OSC sequences, etc.).
// eslint-disable-next-line no-control-regex
const ANSI_PATTERN = /[\u001B\u009B][[\]()#;?]*(?:(?:(?:[a-zA-Z\d]*(?:;[-a-zA-Z\d/#&.:=?%@~_]*)*)?\u0007)|(?:(?:\d{1,4}(?:;\d{0,4})*)?[\dA-PR-TZcf-nq-uy=><~]))/g

/** Strip ANSI escape sequences and normalize carriage returns. */
export function stripAnsi(text: string): string {
  if (!text) return ''
  return text
    .replace(ANSI_PATTERN, '')
    // Collapse standalone carriage returns used for progress bars.
    .replace(/\r(?!\n)/g, '')
}

// ─────────────────────────── Shell results ───────────────────────────

export interface ShellCommandResult {
  command: string
  stdout: string
  stderr: string
  exit_code: number
  timed_out: boolean
}

/**
 * Normalize a `shell` tool result into a typed list.
 * Accepts an already-parsed array/object or a JSON string and returns null
 * when the payload does not look like a shell result.
 */
export function parseShellResults(raw: unknown): ShellCommandResult[] | null {
  let value: any = raw

  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed.startsWith('[') && !trimmed.startsWith('{')) return null
    try {
      value = JSON.parse(trimmed)
    } catch {
      return null
    }
  }

  const list = Array.isArray(value) ? value : [value]
  const looksLikeShell = list.every(
    (item) => item && typeof item === 'object' && 'command' in item && 'exit_code' in item,
  )
  if (!looksLikeShell || list.length === 0) return null

  return list.map((item: any) => ({
    command: String(item.command ?? ''),
    stdout: stripAnsi(String(item.stdout ?? '')),
    stderr: stripAnsi(String(item.stderr ?? '')),
    exit_code: Number(item.exit_code ?? 0),
    timed_out: Boolean(item.timed_out),
  }))
}

// ─────────────────────────── Patch / diff parsing ───────────────────────────

export type DiffLineType = 'add' | 'remove' | 'context' | 'hunk'

export interface DiffLine {
  type: DiffLineType
  text: string
}

export interface FileDiff {
  path: string
  changeType: 'create' | 'update' | 'delete'
  lines: DiffLine[]
  added: number
  removed: number
}

function classifyDiffLine(line: string): DiffLine {
  if (line.startsWith('@@')) return { type: 'hunk', text: line }
  if (line.startsWith('+')) return { type: 'add', text: line.slice(1) }
  if (line.startsWith('-')) return { type: 'remove', text: line.slice(1) }
  // A leading space marks unchanged context; keep the rest verbatim.
  return { type: 'context', text: line.startsWith(' ') ? line.slice(1) : line }
}

function tallyDiff(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const l of lines) {
    if (l.type === 'add') added++
    else if (l.type === 'remove') removed++
  }
  return { added, removed }
}

/**
 * Parse an apply_patch payload (the custom "*** Begin Patch" envelope) into
 * one FileDiff per touched file.
 */
export function parsePatch(patch: string): FileDiff[] {
  if (!patch) return []

  const files: FileDiff[] = []
  let current: FileDiff | null = null

  const flush = () => {
    if (current) {
      const { added, removed } = tallyDiff(current.lines)
      current.added = added
      current.removed = removed
      files.push(current)
    }
  }

  for (const rawLine of patch.split('\n')) {
    const line = rawLine.replace(/\r$/, '')

    if (line.startsWith('*** Begin Patch') || line.startsWith('*** End Patch')) {
      continue
    }

    const fileHeader = line.match(/^\*\*\* (Add|Update|Delete) File:\s*(.+)$/)
    if (fileHeader) {
      flush()
      const action = fileHeader[1]
      // A trailing "@@" hunk marker can be glued to the path; split it off.
      const path = fileHeader[2].replace(/@@.*$/, '').trim()
      current = {
        path,
        changeType: action === 'Add' ? 'create' : action === 'Delete' ? 'delete' : 'update',
        lines: [],
        added: 0,
        removed: 0,
      }
      continue
    }

    if (!current) continue
    current.lines.push(classifyDiffLine(line))
  }

  flush()
  return files
}

/** Build a single FileDiff for a create_file payload (all lines added). */
export function parseCreateFile(path: string, content: string): FileDiff[] {
  const lines: DiffLine[] = (content ?? '')
    .split('\n')
    .map((text) => ({ type: 'add' as const, text }))
  return [{ path, changeType: 'create', lines, added: lines.length, removed: 0 }]
}

// ─────────────────────────── Todo / write_todo parsing ───────────────────────────

export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'deleted'

export interface ParsedTodoItem {
  /** Stable identity (falls back to 1-based array index when the source omits an id). */
  id: number
  content: string
  status: TodoStatus
  /** Optional "in-flight" form, e.g. "正在执行 X" — typically identical to content
   *  but with present-continuous framing. Shown beneath content when present. */
  activeForm?: string
}

/**
 * Best-effort todo extractor that copes with every shape the backend can ship:
 *
 *  1. ACP `todo.update` event payload      — { tasks: [{id, content, status}, ...], total, completed, in_progress }
 *  2. `write_todo` tool-call arguments     — { todos: "[{...}]" | [{...}], merge: bool }
 *  3. `write_todo` tool-call result string — "\"{tasks: [...]}\" | "[{...}]" | formatted summary text
 *  4. Free-form JSON object emitted by the LLM as text — { tool: "write_todo", items: [{content, status, activeForm}, ...] }
 *
 * Returns a normalized `ParsedTodoItem[]` (with stable ids) or an empty array when
 * nothing todo-shaped is detected. Never throws — unparseable inputs degrade
 * gracefully so a single bad payload can't break the timeline.
 */
export function parseTodoItems(raw: unknown): ParsedTodoItem[] {
  if (raw === undefined || raw === null) return []

  let candidate: unknown = raw
  if (typeof candidate === 'string') {
    const trimmed = candidate.trim()
    if (!trimmed) return []
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        candidate = JSON.parse(trimmed)
      } catch {
        // Plain-text tool summary ("Todo list updated — 5 item(s) processed ..."):
        // not a structured payload, so no items to extract.
        return []
      }
    } else {
      return []
    }
  }

  if (!candidate || typeof candidate !== 'object') return []

  const obj = candidate as Record<string, unknown>

  // Shape 1: ACP todo.update event payload.
  if (Array.isArray(obj.tasks)) {
    return normalizeTodoList(obj.tasks)
  }

  // Shape 2: write_todo tool-call arguments.
  if (obj.todos !== undefined) {
    const list = parseStringyList(obj.todos)
    if (list) return normalizeTodoList(list)
  }

  // Shape 4: LLM-emitted JSON tool call as text — { tool: "write_todo", items: [...] }.
  if (Array.isArray(obj.items)) {
    return normalizeTodoList(obj.items)
  }

  // Nested: tool result might wrap the list under a known key.
  for (const key of ['result', 'data', 'payload']) {
    const nested = obj[key]
    if (nested && typeof nested === 'object') {
      const items = parseTodoItems(nested)
      if (items.length > 0) return items
    }
  }

  return []
}

function parseStringyList(value: unknown): unknown[] | null {
  if (Array.isArray(value)) return value
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  if (!trimmed) return null
  try {
    const parsed = JSON.parse(trimmed)
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function normalizeTodoList(raw: unknown[]): ParsedTodoItem[] {
  const items: ParsedTodoItem[] = []
  raw.forEach((entry, idx) => {
    if (!entry || typeof entry !== 'object') return
    const e = entry as Record<string, unknown>
    const content = String(e.content ?? e.text ?? e.title ?? '').trim()
    if (!content) return
    const status = normalizeTodoStatus(e.status)
    const activeForm =
      typeof e.activeForm === 'string' && e.activeForm.trim()
        ? e.activeForm.trim()
        : undefined
    const idRaw = e.id
    const id =
      typeof idRaw === 'number' && Number.isFinite(idRaw) ? idRaw : idx + 1
    items.push({ id, content, status, activeForm })
  })
  return items
}

function normalizeTodoStatus(value: unknown): TodoStatus {
  if (typeof value !== 'string') return 'pending'
  const v = value.toLowerCase().trim()
  if (v === 'completed' || v === 'complete' || v === 'done' || v === 'finished') {
    return 'completed'
  }
  if (v === 'in_progress' || v === 'in-progress' || v === 'inprogress' || v === 'active' || v === 'running') {
    return 'in_progress'
  }
  if (v === 'deleted' || v === 'cancelled' || v === 'canceled' || v === 'removed') {
    return 'deleted'
  }
  return 'pending'
}
