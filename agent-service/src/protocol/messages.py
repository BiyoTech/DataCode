"""Agent Client Protocol v2 (ACP) — 完整事件类型定义。

实现设计文档中定义的 ACP v2 协议，所有 Agent 活动建模为类型化事件流。
前端将这些事件渲染为交互式 Agent Timeline。

事件分类:
  - task.*      : 任务生命周期 (created/running/completed/failed)
  - agent.*     : Agent 状态 (start/finish/delegate)
  - thought.*   : Agent 推理过程 (plan/update)
  - tool.*      : 工具执行 (start/progress/end/error)
  - artifact.*  : 生成物 (created/diff/file/image/chart)
  - human.*     : 人工介入 (required/response)
  - message.*   : 文本消息 (delta/complete)
  - system.*    : 系统通知 (warning/error)

Client → Server:
  - chat.message
  - chat.cancel
  - action.confirm
  - action.reject
  - context.file_change
  - context.file_open
"""

import json
import typing as t
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# ============================================================
# Client → Server 消息类型
# ============================================================

class ClientMessageType(str, Enum):
    """客户端发送到服务端的消息类型。"""
    CHAT_MESSAGE = "chat.message"
    CHAT_CANCEL = "chat.cancel"
    ACTION_CONFIRM = "action.confirm"
    ACTION_REJECT = "action.reject"
    CONTEXT_FILE_CHANGE = "context.file_change"
    CONTEXT_FILE_OPEN = "context.file_open"


@dataclass
class ClientMessage:
    """解析后的客户端消息。"""
    type: ClientMessageType
    session_id: str
    payload: t.Dict[str, t.Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: str) -> "ClientMessage":
        """从 JSON 字符串解析为 ClientMessage。"""
        obj = json.loads(data)
        return cls(
            type=ClientMessageType(obj["type"]),
            session_id=obj.get("session_id", ""),
            payload=obj.get("payload", {}),
        )


# ============================================================
# Server → Client 事件类型 (ACP v2 Event Taxonomy)
# ============================================================

class EventType(str, Enum):
    """ACP v2 事件类型枚举 — 6 大语义分类。"""

    # Task 事件 — 任务生命周期
    TASK_CREATED = "task.created"
    TASK_RUNNING = "task.running"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    # Agent 事件 — Agent 状态变化
    AGENT_START = "agent.start"
    AGENT_FINISH = "agent.finish"
    AGENT_DELEGATE = "agent.delegate"

    # Thought 事件 — Agent 推理过程
    THOUGHT_PLAN = "thought.plan"
    THOUGHT_UPDATE = "thought.update"

    # Tool 事件 — 工具执行
    TOOL_START = "tool.start"
    TOOL_PROGRESS = "tool.progress"
    TOOL_END = "tool.end"
    TOOL_ERROR = "tool.error"

    # Artifact 事件 — 生成物
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_DIFF = "artifact.diff"
    ARTIFACT_FILE = "artifact.file"
    ARTIFACT_IMAGE = "artifact.image"
    ARTIFACT_CHART = "artifact.chart"

    # Human 事件 — 人工介入
    HUMAN_REQUIRED = "human.required"
    HUMAN_RESPONSE = "human.response"

    # Message 事件 — 文本消息流
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETE = "message.complete"

    # Todo 事件 — task list updates
    TODO_UPDATE = "todo.update"

    # System 事件 — 系统通知
    SYSTEM_WARNING = "system.warning"
    SYSTEM_ERROR = "system.error"


@dataclass
class ACPEvent:
    """ACP v2 事件 — 所有服务端事件的统一信封格式。

    Attributes:
        type: 事件类型 (category.event)
        session_id: 所属会话 ID
        event_id: 事件唯一 ID
        parent_event_id: 父事件 ID (用于嵌套关系)
        timestamp: 事件时间戳 (ISO 8601)
        payload: 事件负载数据
    """
    type: EventType
    session_id: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_event_id: t.Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    payload: t.Dict[str, t.Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps({
            "type": self.type.value,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "parent_event_id": self.parent_event_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, data) -> "ACPEvent":
        """Rebuild an event from its JSON form (used by the Redis fan-out).

        Accepts the event as a nested object (the fan-out envelope embeds it
        parsed) or as a JSON string. Raises ValueError on an unknown event
        type — e.g. a rolling upgrade where a newer replica publishes a type
        this process does not know yet. Callers treat that as a dropped
        message rather than an error: the event also reaches clients through
        the producing replica directly.
        """
        obj = data if isinstance(data, dict) else json.loads(data)
        return cls(
            type=EventType(obj["type"]),
            session_id=obj.get("session_id", ""),
            event_id=obj.get("event_id", ""),
            parent_event_id=obj.get("parent_event_id"),
            timestamp=obj.get("timestamp", ""),
            payload=obj.get("payload", {}),
        )


# ============================================================
# 便捷构造函数 — 按事件分类组织
# ============================================================

def create_event(
    event_type: EventType,
    session_id: str,
    payload: t.Optional[t.Dict[str, t.Any]] = None,
    parent_event_id: t.Optional[str] = None,
) -> ACPEvent:
    """通用事件构造函数。"""
    return ACPEvent(
        type=event_type,
        session_id=session_id,
        payload=payload or {},
        parent_event_id=parent_event_id,
    )


# --- Agent 事件 ---

def agent_start(
    session_id: str,
    agent_name: str,
    mode: str = "ask",
    task_id: str = "",
) -> ACPEvent:
    """Agent 开始执行。"""
    return create_event(EventType.AGENT_START, session_id, {
        "agent_name": agent_name,
        "mode": mode,
        "task_id": task_id,
    })


def agent_finish(
    session_id: str,
    agent_name: str,
    outcome: str = "success",
    summary: str = "",
) -> ACPEvent:
    """Agent 执行完成。

    TERMINAL invariant: this event must only be emitted where no further work
    follows on the current turn (goal-loop finalizer, cancel, max-turns/budget
    pause after the auto-resume budget is exhausted, resume failure). The
    frontend treats it as the one event that releases the composer's busy
    state — message.complete is NOT usable for that because it is emitted
    mid-run to seal a streamed text segment. Emitting agent_finish mid-run
    (e.g. between handoffs) would return the send button to idle while the
    task is still running.
    """
    return create_event(EventType.AGENT_FINISH, session_id, {
        "agent_name": agent_name,
        "outcome": outcome,
        "summary": summary,
    })


def agent_delegate(
    session_id: str,
    from_agent: str,
    to_agent: str,
    reason: str = "",
) -> ACPEvent:
    """Agent 委托 (Handoff)。"""
    return create_event(EventType.AGENT_DELEGATE, session_id, {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "reason": reason,
    })


# --- Thought 事件 ---

def thought_plan(
    session_id: str,
    agent_name: str,
    plan: t.List[str],
) -> ACPEvent:
    """Agent 推理计划。"""
    return create_event(EventType.THOUGHT_PLAN, session_id, {
        "agent_name": agent_name,
        "plan": plan,
        "estimated_steps": len(plan),
    })


def thought_update(
    session_id: str,
    agent_name: str,
    thought: str,
) -> ACPEvent:
    """Agent 推理更新。"""
    return create_event(EventType.THOUGHT_UPDATE, session_id, {
        "agent_name": agent_name,
        "thought": thought,
    })


# --- Tool 事件 ---

def tool_start(
    session_id: str,
    tool_name: str,
    agent_name: str = "",
    arguments: t.Optional[t.Dict] = None,
    task_id: str = "",
) -> ACPEvent:
    """工具开始执行。"""
    return create_event(EventType.TOOL_START, session_id, {
        "tool_name": tool_name,
        "agent_name": agent_name,
        "arguments": arguments or {},
        "task_id": task_id,
    })


def tool_progress(
    session_id: str,
    tool_name: str,
    message: str = "",
    progress: float = 0.0,
) -> ACPEvent:
    """工具执行进度。"""
    return create_event(EventType.TOOL_PROGRESS, session_id, {
        "tool_name": tool_name,
        "message": message,
        "progress": progress,
    })


def tool_end(
    session_id: str,
    tool_name: str,
    result: t.Any = None,
    duration_ms: int = 0,
) -> ACPEvent:
    """工具执行完成。"""
    return create_event(EventType.TOOL_END, session_id, {
        "tool_name": tool_name,
        "result": result,
        "duration_ms": duration_ms,
    })


def tool_error(
    session_id: str,
    tool_name: str,
    error: str,
    code: str = "TOOL_ERROR",
    recoverable: bool = True,
) -> ACPEvent:
    """工具执行错误。"""
    return create_event(EventType.TOOL_ERROR, session_id, {
        "tool_name": tool_name,
        "error": error,
        "code": code,
        "recoverable": recoverable,
    })


# --- Artifact 事件 ---

def artifact_created(
    session_id: str,
    file_path: str,
    content: str,
    language: str = "sql",
    task_id: str = "",
) -> ACPEvent:
    """文件生成物创建。"""
    return create_event(EventType.ARTIFACT_CREATED, session_id, {
        "artifact_id": str(uuid.uuid4()),
        "artifact_type": "file",
        "file_path": file_path,
        "language": language,
        "content": content,
        "task_id": task_id,
    })


def artifact_diff(
    session_id: str,
    file_path: str,
    diff: str,
    change_type: str = "update",
    lines_added: int = 0,
    lines_removed: int = 0,
    old_content: str = "",
    new_content: str = "",
    language: str = "diff",
    task_id: str = "",
) -> ACPEvent:
    """File diff artifact.

    Carries everything the UI needs to render a Cursor-style file-change card:
    the change type, added/removed line counts, the unified diff for inline
    preview, and the full before/after content for the side-by-side diff viewer.
    """
    return create_event(EventType.ARTIFACT_DIFF, session_id, {
        "artifact_id": str(uuid.uuid4()),
        "file_path": file_path,
        "language": language,
        "diff": diff,
        "change_type": change_type,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "old_content": old_content,
        "new_content": new_content,
        "task_id": task_id,
    })


# --- Todo 事件 ---

def todo_update(
    session_id: str,
    tasks: t.List[t.Dict[str, t.Any]],
) -> "ACPEvent":
    """Todo list snapshot event.

    Emitted by the write_todo tool after creating/updating tasks.
    Carries the full task board snapshot so the UI can render a
    Cursor-style task list card with progress indicators.
    """
    total = len(tasks)
    completed = sum(1 for t_ in tasks if t_.get("status") == "completed")
    in_progress = sum(1 for t_ in tasks if t_.get("status") == "in_progress")
    return create_event(EventType.TODO_UPDATE, session_id, {
        "tasks": tasks,
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
    })


# --- Human 事件 ---

def human_required(
    session_id: str,
    request_id: str,
    request_type: str,
    title: str,
    description: str = "",
    tool_name: str = "",
    tool_level: int = 2,
    risk_badge_label: str = "L2",
    risk_badge_tone: str = "low",
    options: t.Optional[t.List[t.Dict]] = None,
    task_id: str = "",
    # Command-level approval extensions (shell command security policy)
    commands: t.Optional[t.List[str]] = None,
    rule_name: str = "",
    reason: str = "",
) -> ACPEvent:
    """需要人工介入。

    Extended with command-level approval fields for shell command security:
      - commands: list of shell commands requiring confirmation
      - rule_name: name of the matched policy rule
      - reason: human-readable reason for the confirm/deny action
    """
    payload: t.Dict[str, t.Any] = {
        "request_id": request_id,
        "request_type": request_type,
        "title": title,
        "description": description,
        "tool_name": tool_name,
        "tool_level": tool_level,
        "risk_badge_label": risk_badge_label,
        "risk_badge_tone": risk_badge_tone,
        "options": options or [
            {"id": "approve", "label": "Approve", "style": "primary"},
            {"id": "cancel", "label": "Cancel", "style": "secondary"},
        ],
        "task_id": task_id,
    }
    # Include command-level fields only when present (backward-compatible)
    if commands:
        payload["commands"] = commands
    if rule_name:
        payload["rule_name"] = rule_name
    if reason:
        payload["reason"] = reason

    return create_event(EventType.HUMAN_REQUIRED, session_id, payload)


def human_response(
    session_id: str,
    request_id: str,
    approved: bool,
    reason: str = "",
) -> ACPEvent:
    """Human approval resolution.

    Persisted so a reloaded session can reconcile the matching human.required
    card to its final approved/rejected state instead of guessing.
    """
    return create_event(EventType.HUMAN_RESPONSE, session_id, {
        "request_id": request_id,
        "approved": approved,
        "reason": reason,
    })


# --- Message 事件 ---

def message_delta(
    session_id: str,
    agent_name: str,
    delta: str,
    task_id: str = "",
) -> ACPEvent:
    """流式文本增量。"""
    return create_event(EventType.MESSAGE_DELTA, session_id, {
        "agent_name": agent_name,
        "delta": delta,
        "task_id": task_id,
    })


def message_complete(
    session_id: str,
    agent_name: str,
    content: str,
    task_id: str = "",
) -> ACPEvent:
    """文本消息完成。"""
    return create_event(EventType.MESSAGE_COMPLETE, session_id, {
        "agent_name": agent_name,
        "content": content,
        "task_id": task_id,
    })


# --- System 事件 ---

def system_warning(
    session_id: str,
    code: str,
    message: str,
    suggestion: str = "",
) -> ACPEvent:
    """系统警告。"""
    return create_event(EventType.SYSTEM_WARNING, session_id, {
        "code": code,
        "message": message,
        "suggestion": suggestion,
    })


def system_error(
    session_id: str,
    code: str,
    message: str,
    recoverable: bool = True,
) -> ACPEvent:
    """系统错误。"""
    return create_event(EventType.SYSTEM_ERROR, session_id, {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    })


# ============================================================
# 向后兼容别名 (旧代码迁移用)
# ============================================================

# 旧 ServerMessage 类型映射到新 ACPEvent
ServerMessage = ACPEvent
ServerMessageType = EventType

def text_delta(session_id: str, content: str) -> ACPEvent:
    """兼容旧接口: 流式文本。"""
    return message_delta(session_id, agent_name="", delta=content)

def text_done(session_id: str, full_content: str) -> ACPEvent:
    """兼容旧接口: 文本完成。"""
    return message_complete(session_id, agent_name="", content=full_content)

def agent_error(session_id: str, error: str, code: str = "INTERNAL") -> ACPEvent:
    """兼容旧接口: 错误。"""
    return system_error(session_id, code=code, message=error)

def agent_done(session_id: str) -> ACPEvent:
    """兼容旧接口: Agent 轮次完成。"""
    return agent_finish(session_id, agent_name="triage", outcome="done")

def tool_call_start(session_id: str, tool_name: str, arguments: t.Dict) -> ACPEvent:
    """兼容旧接口: 工具开始。"""
    return tool_start(session_id, tool_name=tool_name, arguments=arguments)

def tool_call_result(session_id: str, tool_name: str, result: t.Any) -> ACPEvent:
    """兼容旧接口: 工具结果。"""
    return tool_end(session_id, tool_name=tool_name, result=result)

def artifact(session_id: str, file_path: str, content: str, language: str = "sql") -> ACPEvent:
    """兼容旧接口: 生成物。"""
    return artifact_created(session_id, file_path=file_path, content=content, language=language)

def action_request(
    session_id: str, action_id: str, action_type: str,
    description: str, tool_level: int = 2,
) -> ACPEvent:
    """兼容旧接口: 动作请求。"""
    risk_badge_tone = "high" if tool_level >= 4 else "medium" if tool_level >= 3 else "low"
    return human_required(
        session_id,
        request_id=action_id,
        request_type=action_type,
        title=action_type,
        description=description,
        tool_level=tool_level,
        risk_badge_label=f"L{tool_level}",
        risk_badge_tone=risk_badge_tone,
    )
