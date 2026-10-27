"""Integration tests for the shared Redis coordination primitives.

Run against a real Redis; skipped when unreachable. A fake would only verify
the mock's own assumptions — what needs pinning is the behaviour on a real
server: SET NX atomicity, pub/sub delivery, CAS unregister.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from datacode_common.redis import (
    DistributedRunRegistry,
    PatternPump,
    get_redis,
    replica_id,
)
from datacode_common.redis.client import redis_key


@pytest.fixture()
async def client():
    # Async on purpose: get_redis() binds a client to the RUNNING loop, and a
    # sync fixture executes outside one.
    c = get_redis()
    if c is None:
        pytest.skip("platform redis disabled")
    try:
        if not await c.ping():
            raise OSError("ping returned false")
    except Exception as exc:
        pytest.skip(f"redis unreachable: {exc}")
    return c


@pytest.fixture(autouse=True)
async def clean_keys(client):
    yield
    keys = [k async for k in client.scan_iter(redis_key("test") + ":*")]
    if keys:
        await client.delete(*keys)


def _channel(suffix: str) -> str:
    return redis_key("test", "pump", suffix)


async def test_pump_delivers_published_messages(client):
    """The core contract: publish → callback on the subscribing loop."""
    received: list[tuple[str, str]] = []
    got = asyncio.Event()

    async def on_message(channel: str, payload: str) -> None:
        received.append((channel, payload))
        got.set()

    pump = PatternPump(
        domain="test", pattern=redis_key("test", "pump") + ":*", on_message=on_message
    )
    await pump.start()
    try:
        # Subscription confirmations are asynchronous; a short poll loop beats
        # a fixed sleep on both slow and fast machines.
        for _ in range(50):
            await client.publish(_channel("a"), "hello")
            try:
                await asyncio.wait_for(got.wait(), timeout=0.1)
                break
            except asyncio.TimeoutError:
                continue
        else:
            pytest.fail("pump never received the published message")

        channel, payload = received[0]
        assert channel == _channel("a")
        assert payload == "hello"
    finally:
        await pump.stop()


async def test_pump_callback_error_does_not_kill_pump(client):
    """A raising callback is contained: the next message still arrives."""
    delivered = asyncio.Event()

    async def on_message(channel: str, payload: str) -> None:
        if payload == "boom":
            raise RuntimeError("callback exploded")
        delivered.set()

    pump = PatternPump(
        domain="test",
        pattern=redis_key("test", "pump") + ":*",
        on_message=on_message,
    )
    await pump.start()
    try:
        for _ in range(50):
            await client.publish(_channel("b"), "boom")
            await asyncio.sleep(0.02)
            await client.publish(_channel("b"), "still-alive")
            try:
                await asyncio.wait_for(delivered.wait(), timeout=0.1)
                return  # pump survived the callback error
            except asyncio.TimeoutError:
                continue
        pytest.fail("pump died after a callback error")
    finally:
        await pump.stop()


async def test_registry_register_is_exclusive_across_instances(client):
    """Two registries stand in for two replicas; only one may register."""
    reg1 = DistributedRunRegistry(domain="test", cancel_local=lambda k: False)
    reg2 = DistributedRunRegistry(domain="test", cancel_local=lambda k: False)
    await reg1.start()
    await reg2.start()
    try:
        run_key = f"run-{uuid.uuid4().hex[:8]}"
        assert await reg1.register(run_key, ttl_seconds=60)
        assert not await reg2.register(run_key, ttl_seconds=60), (
            "second replica must lose the SET NX race"
        )
        assert await reg2.exists(run_key)
    finally:
        await reg1.stop()
        await reg2.stop()


async def test_registry_unregister_is_cas(client):
    """A stale replica cannot delete a successor's registration."""
    reg1 = DistributedRunRegistry(domain="test", cancel_local=lambda k: False)
    await reg1.start()
    try:
        run_key = f"run-{uuid.uuid4().hex[:8]}"
        assert await reg1.register(run_key, ttl_seconds=60)

        # Simulate the stale-holder scenario: overwrite the registration as if
        # TTL expiry + re-registration had happened on another replica.
        await client.set(
            redis_key("test", "run", run_key), "other-replica", ex=60
        )
        await reg1.unregister(run_key)  # must NOT delete "other-replica"
        assert await reg1.exists(run_key)

        # And a matching value does delete.
        await client.set(
            redis_key("test", "run", run_key), replica_id(), ex=60
        )
        await reg1.unregister(run_key)
        assert not await reg1.exists(run_key)
    finally:
        await reg1.stop()


async def test_registry_cancel_reaches_owner_replica(client):
    """A cancel broadcast is executed only by the replica owning the run."""
    cancelled_on_owner = asyncio.Event()
    cancelled_keys: list[str] = []

    def cancel_on_replica1(run_key: str) -> bool:
        # Replica 1 owns the run; replica 2 does not.
        if run_key in replica1_runs:
            cancelled_keys.append(run_key)
            cancelled_on_owner.set()
            return True
        return False

    replica1_runs: set[str] = set()

    reg1 = DistributedRunRegistry(domain="test", cancel_local=cancel_on_replica1)
    # Replica 2 cancels nothing locally: its cancel_local always returns False,
    # forcing the broadcast path.
    reg2 = DistributedRunRegistry(domain="test", cancel_local=lambda k: False)
    await reg1.start()
    await reg2.start()
    try:
        run_key = f"run-{uuid.uuid4().hex[:8]}"
        replica1_runs.add(run_key)
        assert await reg1.register(run_key, ttl_seconds=60)

        # Give both pumps a moment to be subscribed before broadcasting.
        for _ in range(50):
            ok = await reg2.cancel(run_key)
            if not ok:
                await asyncio.sleep(0.05)
                continue
            break
        else:
            pytest.fail("cancel broadcast never reached any subscriber")

        # The owner executed the cancel.
        await asyncio.wait_for(cancelled_on_owner.wait(), timeout=2.0)
        assert run_key in cancelled_keys
    finally:
        await reg1.stop()
        await reg2.stop()
