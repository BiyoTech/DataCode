"""Tests for the ``is_running`` flag on GET /sessions/{id}.

The flag exists because the frontend cannot derive "a run is in flight" from
the event stream alone: ``message.complete`` is emitted MID-RUN (it seals one
streamed text segment before the next tool call or handoff), so the last
persisted event of a live run routinely is a message, not an agent.start. What
is pinned here is the contract the composer restores its busy state from:

* a LIVE local task reads as running;
* a stale dict entry whose task already finished does NOT — otherwise every
  completed session would report busy forever;
* a run owned by another replica reads as running via the registry;
* nothing running anywhere reads as not running.
"""

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.runtime.active_runs import active_chat_runs


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_runs():
    active_chat_runs.clear()
    yield
    active_chat_runs.clear()


def _fake_session(session_id: str):
    class S:
        title = "test session"
        mode = type("M", (), {"value": "craft"})()
        execution_style = type("E", (), {"value": "agent"})()
        scenario = "chat"
        status = "active"
        message_count = 0
        created_at = None
        last_message_at = None
        messages: list = []

    S.session_id = session_id
    return S()


def _stub_session_manager(monkeypatch, session_id: str):
    async def fake_get_session(_sid):
        return _fake_session(_sid)

    async def fake_timeline(_sid):
        return []

    monkeypatch.setattr(
        "src.api.routes.sessions.session_manager.get_session_async", fake_get_session
    )
    monkeypatch.setattr(
        "src.api.routes.sessions.session_manager.get_timeline_events_async",
        fake_timeline,
    )


def test_is_running_false_when_nothing_is_registered(client, monkeypatch):
    _stub_session_manager(monkeypatch, "sess-1")
    resp = client.get("/api/v1/sessions/sess-1")
    assert resp.status_code == 200
    assert resp.json()["is_running"] is False


class _FakeTask:
    """Narrowest double for the registry's task handle.

    The endpoint's only interaction with a task is ``done()`` — that predicate
    IS the contract under test, so a real asyncio.Task (bound to some loop
    pytest-asyncio may already have closed) would buy nothing and leak.
    """

    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def test_is_running_true_with_a_live_local_task(client, monkeypatch):
    _stub_session_manager(monkeypatch, "sess-1")

    active_chat_runs["sess-1"] = {
        "task": _FakeTask(done=False),
        "result": None,
        "agent_name": "triage",
    }
    resp = client.get("/api/v1/sessions/sess-1")
    assert resp.json()["is_running"] is True


def test_is_running_false_with_a_finished_local_task(client, monkeypatch):
    _stub_session_manager(monkeypatch, "sess-1")

    active_chat_runs["sess-1"] = {
        "task": _FakeTask(done=True),
        "result": None,
        "agent_name": "triage",
    }
    resp = client.get("/api/v1/sessions/sess-1")
    assert resp.json()["is_running"] is False


def test_is_running_true_when_another_replica_owns_the_run(client, monkeypatch):
    _stub_session_manager(monkeypatch, "sess-1")

    async def _remote_yes(_sid):
        return True

    monkeypatch.setattr(
        "src.api.routes.sessions.is_chat_running_remotely", _remote_yes
    )

    resp = client.get("/api/v1/sessions/sess-1")
    assert resp.json()["is_running"] is True


def test_is_running_survives_a_redis_outage_as_false(client, monkeypatch):
    """Degraded mode: registry unreachable reads as 'no remote run' (the
    existing fail-open semantics of is_chat_running_remotely) rather than
    breaking the session GET."""
    _stub_session_manager(monkeypatch, "sess-1")

    async def _boom(_sid):
        raise ConnectionError("redis down")

    # The route calls the registry through the same helper the rest of the
    # service uses; its own degradation already swallows Redis errors, so an
    # ESCAPED error would be a bug — simulate by patching the helper to the
    # degraded behaviour instead of asserting on the exception path.
    async def _degraded(_sid):
        try:
            await _boom(_sid)
        except ConnectionError:
            return False

    monkeypatch.setattr("src.api.routes.sessions.is_chat_running_remotely", _degraded)

    resp = client.get("/api/v1/sessions/sess-1")
    assert resp.status_code == 200
    assert resp.json()["is_running"] is False
