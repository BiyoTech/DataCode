"""Lifecycle of a chat run's cross-replica registration.

The registration is what refuses a second POST /chat for a session (409). It
must therefore die with the run that owns it: a registration that outlives its
task locks the session out until the Redis TTL expires, and the user has no way
to tell the difference between "still working" and "permanently stuck".

These tests pin the contract that the release does NOT depend on
``active_chat_runs`` still holding the task — the regression that leaked the
key was exactly that: the goal loop dropped the local handle before the task
finished, so the completion callback skipped the release.
"""

import asyncio

import pytest

from src.api.schemas import ChatMessageRequest
from src.runtime import active_runs
from src.runtime.active_runs import active_chat_runs, track_chat_run


class FakeRegistry:
    """In-memory stand-in for DistributedRunRegistry (one replica)."""

    def __init__(self):
        self.keys: set[str] = set()
        self.refreshes: list[str] = []

    async def register(self, run_key: str, *, ttl_seconds: int) -> bool:
        if run_key in self.keys:
            return False
        self.keys.add(run_key)
        return True

    async def refresh(self, run_key: str, *, ttl_seconds: int) -> None:
        self.refreshes.append(run_key)

    async def unregister(self, run_key: str) -> None:
        self.keys.discard(run_key)

    async def exists(self, run_key: str) -> bool:
        return run_key in self.keys

    async def cancel(self, run_key: str) -> bool:
        return active_runs._cancel_local_run(run_key)


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(active_runs, "_registry", fake)
    active_chat_runs.clear()
    yield fake
    active_chat_runs.clear()


async def _settle():
    """Let the done callback's fire-and-forget release task run."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_release_does_not_depend_on_the_local_handle(registry):
    """The run itself dropping its handle must not strand the registration."""
    session_id = "sess-drops-handle"
    assert await active_runs.register_chat_run(session_id)

    async def run():
        # Exactly what the goal loop used to do in a `finally` block.
        active_chat_runs.pop(session_id, None)

    task = asyncio.create_task(run())
    track_chat_run(session_id, task, agent_name="ask")
    await task
    await _settle()

    assert registry.keys == set()


async def test_release_on_failure(registry):
    session_id = "sess-fails"
    assert await active_runs.register_chat_run(session_id)

    async def run():
        raise RuntimeError("model exploded")

    task = asyncio.create_task(run())
    track_chat_run(session_id, task, agent_name="ask")
    with pytest.raises(RuntimeError):
        await task
    await _settle()

    assert registry.keys == set()
    assert session_id not in active_chat_runs


async def test_release_on_cancellation(registry):
    session_id = "sess-cancelled"
    assert await active_runs.register_chat_run(session_id)

    async def run():
        await asyncio.sleep(60)

    task = asyncio.create_task(run())
    track_chat_run(session_id, task, agent_name="ask")
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert registry.keys == set()


async def test_newer_run_keeps_its_registration(registry):
    """A finishing task must not release a registration it no longer owns."""
    session_id = "sess-superseded"
    assert await active_runs.register_chat_run(session_id)

    release = asyncio.Event()

    async def run():
        await release.wait()

    old = asyncio.create_task(run())
    track_chat_run(session_id, old, agent_name="ask")
    await asyncio.sleep(0)

    # A newer run takes the session over while the old task is still winding
    # down (forwarded resume racing the tail of the previous run).
    active_chat_runs[session_id] = {"task": object(), "result": None, "agent_name": "ask"}
    release.set()
    await old
    await _settle()

    assert registry.keys == {session_id}


async def test_cancel_releases_a_stale_registration(registry):
    """Cancel is the escape hatch when a registration outlived its replica."""
    session_id = "sess-stale"
    assert await active_runs.register_chat_run(session_id)
    assert session_id not in active_chat_runs  # nothing running here

    await active_runs.cancel_chat_run(session_id)

    assert registry.keys == set()


async def test_second_message_is_accepted_after_the_first_run_ends(registry, monkeypatch):
    """End-to-end shape of the reported bug: message 2 must not get a 409."""
    from src.api.routes import chat as chat_route

    session_id = "sess-http"

    class _Session:
        session_id = "sess-http"
        mode = type("M", (), {"value": "ask"})()

    async def fake_get_session(_session_id):
        return _Session()

    async def fake_execute_chat(_session, _req):
        # Mimic the goal loop clearing its own handle before returning.
        active_chat_runs.pop(session_id, None)

    monkeypatch.setattr(chat_route.session_manager, "get_session_async", fake_get_session)
    monkeypatch.setattr(chat_route, "execute_chat", fake_execute_chat)

    req = ChatMessageRequest(content="hello")

    first = await chat_route.post_chat_message(session_id, req)
    assert first["status"] == "accepted"
    await _settle()
    assert registry.keys == set()

    second = await chat_route.post_chat_message(session_id, req)
    assert second["status"] == "accepted"
    await _settle()
    assert registry.keys == set()
