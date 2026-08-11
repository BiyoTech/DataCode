"""Tests for timeline publish_and_persist."""

import pytest

from src.protocol.messages import EventType, create_event, message_delta
from src.runtime.timeline import TIMELINE_PERSISTABLE_EVENTS, publish_and_persist


@pytest.mark.asyncio
async def test_delta_not_persisted(monkeypatch):
    published = []
    persisted = []

    async def fake_publish(session_id, event):
        published.append(event.type)

    async def fake_persist(**kwargs):
        persisted.append(kwargs["event_type"])

    monkeypatch.setattr("src.runtime.timeline.event_bus.publish", fake_publish)
    monkeypatch.setattr(
        "src.runtime.timeline.session_manager.add_timeline_event_async",
        fake_persist,
    )

    session_id = "s1"
    event = message_delta(session_id, agent_name="triage", delta="hi")
    await publish_and_persist(session_id, event)

    assert published == [EventType.MESSAGE_DELTA]
    assert persisted == []


@pytest.mark.asyncio
async def test_persistable_event_written(monkeypatch):
    persisted = []

    async def fake_publish(session_id, event):
        pass

    async def fake_persist(**kwargs):
        persisted.append(kwargs["event_type"])

    monkeypatch.setattr("src.runtime.timeline.event_bus.publish", fake_publish)
    monkeypatch.setattr(
        "src.runtime.timeline.session_manager.add_timeline_event_async",
        fake_persist,
    )

    event = create_event(EventType.AGENT_START, "s1", {"agent_name": "triage"})
    assert event.type in TIMELINE_PERSISTABLE_EVENTS
    await publish_and_persist("s1", event)
    assert persisted == [EventType.AGENT_START.value]
