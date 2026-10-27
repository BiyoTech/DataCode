"""Unit tests for cross-replica session coordination.

Covers the routing decisions (local-first, then remote existence check, then
broadcast) and the control-message dispatch, with the Redis client faked so the
tests run without a live Redis. The pump pipeline itself (pattern subscription,
origin filtering) is covered by the shared redis coordination tests; here it is
bypassed by calling ``_on_message`` directly.
"""

from __future__ import annotations

import asyncio
import json
import typing as t

import pytest
from datacode_common.redis.client import redis_key

from src.runtime.session_coordination import (
    KIND_RESUME,
    KIND_SDK_APPROVAL,
    SessionCoordination,
)


class FakeRedis:
    """Async fake covering the surface SessionCoordination uses."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex or 0
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                self.store.pop(key)
                removed += 1
        return removed

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 1


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def coordination(fake_redis) -> SessionCoordination:
    return SessionCoordination(client=fake_redis)


def _message(action: str, **fields: t.Any) -> str:
    # Origin is foreign so the handler does not self-filter.
    return json.dumps({"origin": "other-replica/123/abcd", "action": action, **fields})


async def test_register_pending_records_owner_and_ttl(fake_redis, coordination):
    await coordination.register_pending(KIND_SDK_APPROVAL, "s1", "r1", 600)
    # Derived, not hardcoded: the namespace now includes the cell id (P1), and
    # a literal prefix here would break again on the next namespacing change.
    key = redis_key("agent", "pending", "approval", "s1", "r1")
    assert fake_redis.store[key]
    assert fake_redis.ttls[key] == 600


async def test_pending_exists_and_unregister(fake_redis, coordination):
    await coordination.register_pending(KIND_RESUME, "s1", "", 1800)
    assert await coordination.pending_exists(KIND_RESUME, "s1") is True

    await coordination.unregister_pending(KIND_RESUME, "s1", "")
    assert await coordination.pending_exists(KIND_RESUME, "s1") is False


async def test_resolve_approval_local_sdk_hit(monkeypatch, fake_redis, coordination):
    """Local resolution wins without any Redis round trip for the answer."""

    async def _noop_publish(*args, **kwargs):
        return None

    import src.runtime.approval as approval_mod

    approval_mod._pending_approvals["s1"] = {
        "r1": object.__new__(approval_mod.PendingApproval),
    }
    monkeypatch.setattr(approval_mod, "resolve_pending_approval", lambda *a, **k: True)
    monkeypatch.setattr(
        "src.runtime.timeline.publish_and_persist", _noop_publish, raising=False
    )
    monkeypatch.setattr(
        "src.tools.command_approval.resolve_command_approval", lambda *a, **k: False
    )

    outcome = await coordination.resolve_approval("s1", "r1", True, "")
    assert outcome == {"status": "resolved", "request_id": "r1", "approved": True}


async def test_resolve_approval_forwards_when_remote_exists(
    monkeypatch, fake_redis, coordination
):
    """No local entry + a remote pending key → broadcast + forwarded status."""
    await fake_redis.set(
        redis_key("agent", "pending", "cmd", "s1", "r9"), "other-replica", ex=300
    )
    monkeypatch.setattr(
        "src.tools.command_approval.resolve_command_approval", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "src.runtime.approval.resolve_pending_approval", lambda *a, **k: False
    )

    outcome = await coordination.resolve_approval("s1", "r9", True, "")
    assert outcome == {"status": "forwarded", "request_id": "r9", "approved": True}
    # The resolution was broadcast on the control channel. The broadcast is a
    # fire-and-forget task; yield long enough for it to reach the fake client.
    await asyncio.sleep(0.05)
    channel, payload = fake_redis.published[-1]
    assert channel.endswith(":agent:session_ctl")
    assert json.loads(payload)["action"] == "resolve_approval"
    # Give the fire-and-forget broadcast task a chance to run.
    await asyncio.sleep(0)


async def test_resolve_approval_returns_none_when_nowhere(monkeypatch, coordination):
    monkeypatch.setattr(
        "src.tools.command_approval.resolve_command_approval", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "src.runtime.approval.resolve_pending_approval", lambda *a, **k: False
    )
    assert await coordination.resolve_approval("s1", "missing", True, "") is None


async def test_invalidation_message_drops_cached_session(
    monkeypatch, coordination, fake_redis
):
    dropped: list[str] = []

    class _FakeManager:
        def invalidate_cached_session(self, session_id: str) -> bool:
            dropped.append(session_id)
            return True

    import src.context.session as session_mod

    monkeypatch.setattr(session_mod, "session_manager", _FakeManager())
    await coordination._on_message("chan", _message("invalidate", session_id="s1"))
    assert dropped == ["s1"]


async def test_own_origin_message_is_ignored(coordination):
    """The local half of every action already ran; the pump filter is the
    second line of defence (origin check in _on_message)."""
    payload = json.dumps({"origin": "me", "action": "invalidate", "session_id": "s1"})
    # Must not raise and must not touch anything.
    await coordination._on_message("chan", payload)


async def test_resolve_approval_message_runs_local_resolution(
    monkeypatch, coordination
):
    """Owner side: a forwarded resolution resolves against the local registry."""
    calls: list[tuple[str, str, bool]] = []

    def _fake_resolve(session_id, request_id, approved):
        calls.append((session_id, request_id, approved))
        return True

    async def _noop_publish(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "src.tools.command_approval.resolve_command_approval", _fake_resolve
    )
    monkeypatch.setattr(
        "src.runtime.timeline.publish_and_persist", _noop_publish, raising=False
    )

    await coordination._on_message(
        "chan",
        _message("resolve_approval", session_id="s1", request_id="r1", approved=True),
    )
    assert calls == [("s1", "r1", True)]


async def test_unknown_action_is_ignored(coordination):
    """Forward compatibility: a newer producer's action must not break us."""
    await coordination._on_message("chan", _message("future_action", session_id="s1"))


async def test_cache_invalidation_broadcast_is_throttled(fake_redis, coordination):
    """A chat turn appends many messages; only the first per window publishes."""
    for _ in range(5):
        coordination.broadcast_cache_invalidation("s1")
        await asyncio.sleep(0)  # let fire-and-forget tasks run
    invalidations = [
        payload
        for _, payload in fake_redis.published
        if json.loads(payload)["action"] == "invalidate"
    ]
    assert len(invalidations) == 1
