"""Timeline event publishing and persistence."""

import typing as t

from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.protocol.messages import ACPEvent, EventType

TIMELINE_PERSISTABLE_EVENTS: t.Set[EventType] = {
    EventType.AGENT_START,
    EventType.AGENT_FINISH,
    EventType.AGENT_DELEGATE,
    EventType.THOUGHT_PLAN,
    EventType.THOUGHT_UPDATE,
    EventType.TOOL_START,
    EventType.TOOL_END,
    EventType.TOOL_ERROR,
    EventType.ARTIFACT_CREATED,
    EventType.ARTIFACT_DIFF,
    EventType.TODO_UPDATE,
    EventType.HUMAN_REQUIRED,
    EventType.MESSAGE_COMPLETE,
    EventType.SYSTEM_WARNING,
    EventType.SYSTEM_ERROR,
}


async def publish_and_persist(session_id: str, event: ACPEvent) -> None:
    """Publish an ACP event to SSE subscribers and persist it to DB when applicable."""
    await event_bus.publish(session_id, event)
    if event.type in TIMELINE_PERSISTABLE_EVENTS:
        await session_manager.add_timeline_event_async(
            session_id=session_id,
            event_type=event.type.value,
            event_id=event.event_id,
            parent_event_id=event.parent_event_id or "",
            payload=event.payload,
        )
