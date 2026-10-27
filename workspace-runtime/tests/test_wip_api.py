"""WIP user-facing API tests (design 07 Q6 / ADR-003).

The contract this API publishes is a product promise, so the tests pin the
BEHAVIOUR the frontend depends on, not the handler plumbing:

* the indicator must never lie: ``enabled: false`` when the lease subsystem is
  off, a 503 (retryable) when this instance is not the holder — never a status
  borrowed from another instance;
* recover/discard are strictly the OWNER's decisions: another member of the
  same workspace gets 403, not a mutated snapshot;
* failure paths keep the prompt up: a failed discard answers 503 so the user
  can retry, and a failed recover answers 409 with the human action needed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.rest import wip as wip_api  # noqa: E402
from src.config import settings  # noqa: E402
from src.lease.wip import RecoveredSnapshot  # noqa: E402

# The working copy these tests address: {base}/t1/ws-t1-shop/u1/envs/dev.
_BASE = "base"
_WORKSPACE_PATH_PARTS = ("t1", "ws-t1-shop", "u1", "envs", "dev")
_LEASE_KEY = "t1/ws-t1-shop/u1/dev"
_USER = "u1"
_OTHER_USER = "u2"


@dataclass
class FakeServedCopy:
    pending_snapshot: Optional[RecoveredSnapshot] = None


class FakeLeaseManager:
    """Just enough manager for the API layer: it only reads and delegates."""

    def __init__(self) -> None:
        self.copies: dict[str, FakeServedCopy] = {}
        self.states: dict[str, dict] = {}
        self.recover_results: dict[str, bool] = {}
        self.discard_results: dict[str, bool] = {}
        self.recover_calls: list[str] = []
        self.discard_calls: list[str] = []

    def copy_protection_status(self, lease_key):
        if lease_key not in self.copies:
            return None
        state = self.states.get(lease_key, {})
        return {
            "protected": state.get("protected", True),
            "last_protected_at": state.get("last_protected_at"),
            "pending_snapshot": self.copies[lease_key].pending_snapshot,
        }

    def served_copy(self, lease_key):
        copy = self.copies.get(lease_key)
        return copy if copy is not None else None

    def holds(self, lease_key):
        # The request-path gate (src.api.deps) asks this before the endpoint
        # runs: a copy this manager serves is by definition held here.
        return lease_key in self.copies

    # The gate also reads the draining flag; these tests never drain.
    draining = False

    async def recover_snapshot(self, lease_key):
        self.recover_calls.append(lease_key)
        return self.recover_results.get(lease_key, True)

    async def discard_snapshot(self, lease_key):
        self.discard_calls.append(lease_key)
        return self.discard_results.get(lease_key, True)


@pytest.fixture()
def workspace_path(tmp_path, monkeypatch):
    """A working-copy directory matching the platform layout."""
    base = tmp_path / _BASE
    target = base.joinpath(*_WORKSPACE_PATH_PARTS)
    target.mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    return str(target)


@pytest.fixture()
def manager(monkeypatch):
    fake = FakeLeaseManager()
    import src.lease.wiring as wiring

    monkeypatch.setattr(wiring, "get_lease_manager", lambda: fake)
    # The handlers import the accessor lazily from the module, so patching the
    # module attribute is what the tests observe.
    monkeypatch.setattr(
        "src.lease.wiring.get_lease_manager", lambda: fake
    )
    return fake


@pytest.fixture()
def client():
    """An app with only the WIP router; identity arrives via headers.

    The ServiceError handler mirrors the production registration in
    ``src.main`` so error responses keep their structured shape here too.
    """
    from fastapi.responses import JSONResponse

    from src.utils.errors import ServiceError

    app = FastAPI()
    app.include_router(wip_api.router)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_response_dict()
        )

    return TestClient(app)


def _headers(user: str = _USER) -> dict:
    return {"X-User-ID": user, "X-Tenant-ID": "t1"}


def _status(client, workspace_path, user=_USER):
    return client.get(
        "/api/v1/wip/status",
        params={"workspace_path": workspace_path},
        headers=_headers(user),
    )


# ── status ─────────────────────────────────────────────────────────────────


def test_status_reports_disabled_when_lease_subsystem_off(
    client, workspace_path, monkeypatch
):
    import src.lease.wiring as wiring

    monkeypatch.setattr(wiring, "get_lease_manager", lambda: None)
    monkeypatch.setattr("src.lease.wiring.get_lease_manager", lambda: None)

    response = _status(client, workspace_path)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    # The frontend keys its polling cadence off this even when disabled.
    assert body["snapshot_interval_seconds"] == settings.wip_snapshot_interval_seconds


def test_status_reports_protection_state(client, workspace_path, manager):
    stamp = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    manager.copies[_LEASE_KEY] = FakeServedCopy()
    manager.states[_LEASE_KEY] = {
        "protected": False,
        "last_protected_at": stamp,
    }

    response = _status(client, workspace_path)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["protected"] is False
    assert body["last_protected_at"] == stamp.isoformat()
    assert body["pending_snapshot"] is None


def test_status_includes_pending_snapshot_with_taken_at(
    client, workspace_path, manager
):
    taken = datetime(2026, 8, 27, 11, 50, 0, tzinfo=timezone.utc)
    manager.copies[_LEASE_KEY] = FakeServedCopy(
        pending_snapshot=RecoveredSnapshot(
            lease_key=_LEASE_KEY,
            commit="abc123",
            ref="refs/datacode-wip/u1/dev",
            taken_at=taken,
        )
    )

    body = _status(client, workspace_path).json()

    assert body["pending_snapshot"] == {
        "ref": "refs/datacode-wip/u1/dev",
        "commit": "abc123",
        "taken_at": taken.isoformat(),
    }


def test_status_answers_lease_not_held_when_not_serving_here(
    client, workspace_path, manager
):
    """Not an error the user can act on: the gateway re-routes and retries.

    The lease gate in ``validate_workspace_path`` answers this before the
    endpoint runs, with the contract code the gateway keys its retry on
    (design 08 §7) — previously it surfaced as a bare 503 the frontend
    could only show as an error.
    """
    response = _status(client, workspace_path)

    assert response.status_code == 409
    assert response.json()["error_code"] == "LEASE_NOT_HELD"


def test_status_is_restricted_to_the_copy_owner(client, workspace_path, manager):
    manager.copies[_LEASE_KEY] = FakeServedCopy()

    response = _status(client, workspace_path, user=_OTHER_USER)

    assert response.status_code == 403


# ── recover / discard ──────────────────────────────────────────────────────


def test_recover_applies_the_pending_snapshot(client, workspace_path, manager):
    manager.copies[_LEASE_KEY] = FakeServedCopy(
        pending_snapshot=RecoveredSnapshot(
            lease_key=_LEASE_KEY, commit="abc123", ref="refs/datacode-wip/u1/dev"
        )
    )

    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"recovered": True}
    assert manager.recover_calls == [_LEASE_KEY]


def test_recover_without_pending_snapshot_is_idempotent(
    client, workspace_path, manager
):
    manager.copies[_LEASE_KEY] = FakeServedCopy()

    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "no_pending_snapshot"
    assert manager.recover_calls == []


def test_recover_conflict_keeps_the_decision_open(client, workspace_path, manager):
    manager.copies[_LEASE_KEY] = FakeServedCopy(
        pending_snapshot=RecoveredSnapshot(
            lease_key=_LEASE_KEY, commit="abc123", ref="refs/datacode-wip/u1/dev"
        )
    )
    manager.recover_results[_LEASE_KEY] = False

    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"


def test_recover_rejects_another_member(client, workspace_path, manager):
    manager.copies[_LEASE_KEY] = FakeServedCopy()

    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(_OTHER_USER),
    )

    assert response.status_code == 403
    assert manager.recover_calls == []


def test_recover_when_lease_subsystem_off_answers_409(
    client, workspace_path, monkeypatch
):
    monkeypatch.setattr("src.lease.wiring.get_lease_manager", lambda: None)

    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 409


def test_recover_when_not_serving_here_answers_lease_not_held(
    client, workspace_path, manager
):
    response = client.post(
        "/api/v1/wip/recover",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "LEASE_NOT_HELD"


def test_discard_drops_the_pending_snapshot(client, workspace_path, manager):
    manager.copies[_LEASE_KEY] = FakeServedCopy(
        pending_snapshot=RecoveredSnapshot(
            lease_key=_LEASE_KEY, commit="abc123", ref="refs/datacode-wip/u1/dev"
        )
    )

    response = client.post(
        "/api/v1/wip/discard",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"discarded": True}
    assert manager.discard_calls == [_LEASE_KEY]


def test_discard_failure_answers_503_so_the_prompt_survives(
    client, workspace_path, manager
):
    """A silent success here would resurrect the prompt on every
    materialization — the exact 'training users to dismiss it' failure the
    WIP module docstring warns about."""
    manager.copies[_LEASE_KEY] = FakeServedCopy(
        pending_snapshot=RecoveredSnapshot(
            lease_key=_LEASE_KEY, commit="abc123", ref="refs/datacode-wip/u1/dev"
        )
    )
    manager.discard_results[_LEASE_KEY] = False

    response = client.post(
        "/api/v1/wip/discard",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 503
    assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"


def test_discard_without_pending_snapshot_is_idempotent(
    client, workspace_path, manager
):
    manager.copies[_LEASE_KEY] = FakeServedCopy()

    response = client.post(
        "/api/v1/wip/discard",
        json={"workspace_path": workspace_path},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "no_pending_snapshot"


def test_missing_identity_is_rejected(client, workspace_path):
    response = client.get(
        "/api/v1/wip/status",
        params={"workspace_path": workspace_path},
    )

    assert response.status_code == 403
