"""Integration tests for the session event bus Redis fan-out.

Runs against a real Redis; skipped when unreachable. The three behaviours
pinned here are what makes SSE work across replicas:

1. a local publish is ALSO visible on the session's Redis channel;
2. a message published by ANOTHER replica (simulated with a foreign origin)
   is delivered into the local subscriber's queue;
3. a message carrying OUR origin is dropped — the local write already
   delivered it, and not filtering would duplicate every local event.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from datacode_common.redis import get_redis, replica_id

from src.context.event_bus import SessionEventBus
from src.protocol.messages import EventType, agent_start


@pytest.fixture()
async def client():
    c = get_redis()
    if c is None:
        pytest.skip("platform redis disabled")
    try:
        if not await c.ping():
            raise OSError("ping returned false")
    except Exception as exc:
        pytest.skip(f"redis unreachable: {exc}")
    return c


@pytest.fixture()
def session_id() -> str:
    return f"fanout-test-{uuid.uuid4().hex[:8]}"


def _channel(session_id: str) -> str:
    from datacode_common.redis.client import redis_config

    return f"{redis_config().key_prefix}:agent:session:{session_id}:events"


async def _wait_for_pump(bus: SessionEventBus) -> None:
    """Wait until the bus's fan-out pump is subscribed and settled.

    attach_subscriber() starts the pump as a fire-and-forget task; the
    subscription takes effect asynchronously. Polling pump.running plus a
    short settle covers the psubscribe round trip; the delivery assertions
    below then verify it for real.
    """
    for _ in range(100):
        fanout = bus._fanout
        if fanout._pump is not None and fanout._pump.running:
            await asyncio.sleep(0.1)
            return
        await asyncio.sleep(0.05)
    pytest.fail("fan-out pump never started")


async def test_local_publish_reaches_redis_channel(client, session_id):
    """Publish must fan out: the channel carries the event envelope."""
    bus = SessionEventBus()
    bus.attach_subscriber(session_id)
    await _wait_for_pump(bus)

    subscriber = client.pubsub()
    await subscriber.subscribe(_channel(session_id))
    try:
        # Drain the subscribe confirmation so it cannot be mistaken for data.
        confirmation = await subscriber.get_message(timeout=1.0)
        assert confirmation is None or confirmation.get("type") == "subscribe"

        await bus.publish(session_id, agent_start(session_id, "triage"))

        msg = await subscriber.get_message(timeout=2.0)
        assert msg is not None, "publish did not reach the Redis channel"
        envelope = json.loads(msg["data"])
        assert envelope["origin"] == replica_id()
        assert envelope["event"]["type"] == EventType.AGENT_START.value
    finally:
        await subscriber.unsubscribe(_channel(session_id))
        await subscriber.aclose()


async def test_remote_message_delivered_to_local_subscriber(client, session_id):
    """The replica-bridging contract: a foreign publish lands in our queue."""
    bus = SessionEventBus()
    sq, token = bus.attach_subscriber(session_id)
    await _wait_for_pump(bus)
    try:
        # Simulate another replica: origin is foreign, the event is a real one.
        event = agent_start(session_id, "remote-agent")
        envelope = json.dumps(
            {"origin": "other-replica-1", "event": event.to_json()},
            ensure_ascii=False,
        )
        await client.publish(_channel(session_id), envelope)

        item = await asyncio.wait_for(sq.queue.get(), timeout=2.0)
        assert item.type == EventType.AGENT_START
        assert item.payload["agent_name"] == "remote-agent"
    finally:
        bus.detach(session_id, token)


async def test_own_origin_message_is_filtered(client, session_id):
    """Without the origin filter every local event would be doubled."""
    bus = SessionEventBus()
    sq, token = bus.attach_subscriber(session_id)
    await _wait_for_pump(bus)
    try:
        envelope = json.dumps(
            {
                "origin": replica_id(),
                "event": agent_start(session_id, "self").to_json(),
            },
            ensure_ascii=False,
        )
        await client.publish(_channel(session_id), envelope)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sq.queue.get(), timeout=0.5)
    finally:
        bus.detach(session_id, token)
