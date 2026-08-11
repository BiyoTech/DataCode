"""Tests for session cancel endpoint."""

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.runtime.active_runs import active_chat_runs


@pytest.fixture
def client():
    return TestClient(app)


def test_cancel_returns_ok_when_no_active_run(client, monkeypatch):
    async def fake_get_session(_session_id):
        class S:
            session_id = "sess-1"
            mode = type("M", (), {"value": "ask"})()

        return S()

    monkeypatch.setattr("src.api.routes.chat.session_manager.get_session_async", fake_get_session)
    active_chat_runs.clear()

    resp = client.post("/api/v1/sessions/sess-1/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
