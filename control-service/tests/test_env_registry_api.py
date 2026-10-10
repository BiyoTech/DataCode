"""``/internal/env-registry/*`` — registry writes performed on the runtime's behalf.

The runtime authors two facts about control-owned tables (an apply's watermark,
a merge's audit row) and sends them here rather than writing them, because two
services writing one table is the ownership break this platform has already
paid for twice.

What must hold:

* the surface is internal-only and FAILS CLOSED — unconfigured token means it
  answers nobody, not everybody;
* the control plane applies its OWN rule: there is no generic "update this
  column" escape hatch through which the runtime could bypass the registry's
  whitelist;
* an unknown environment is reported, not raised: the caller treats this as
  bookkeeping that must never fail the operation it describes.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.rest import env_registry
from src.config import settings

_TOKEN = "internal-test-token"


@dataclass
class _FakeRepository:
    """Records what the endpoint asked the registry to do."""

    updates: list = field(default_factory=list)
    events: list = field(default_factory=list)
    update_result: bool = True

    async def update(self, environment_id: str, **values: t.Any) -> bool:
        self.updates.append((environment_id, values))
        return self.update_result

    async def record_event(self, **kwargs: t.Any) -> str:
        self.events.append(kwargs)
        return "ev-1"


@pytest.fixture()
def wired(monkeypatch):
    repo = _FakeRepository()
    monkeypatch.setattr(env_registry, "EnvironmentRepository", lambda: repo)
    monkeypatch.setattr(settings, "runtime_internal_token", _TOKEN)

    app = FastAPI()
    app.include_router(env_registry.router)
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    )
    return client, repo


async def test_applied_watermark_reaches_the_registry(wired):
    client, repo = wired

    response = await client.post(
        "/internal/env-registry/applied",
        json={"environment_id": "ws-t1-shop:prod", "commit_hash": "abc", "dirty": True},
        headers={"X-Internal-Token": _TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"updated": True}
    environment_id, values = repo.updates[0]
    assert environment_id == "ws-t1-shop:prod"
    assert values["last_applied_commit"] == "abc"
    assert values["last_applied_dirty"] == 1
    # Stamped by the control plane when the caller does not supply one.
    assert values["last_applied_at"] is not None


async def test_an_empty_commit_is_stored_as_null(wired):
    """A dirty apply with no commit must not record the empty string."""
    client, repo = wired

    await client.post(
        "/internal/env-registry/applied",
        json={"environment_id": "e", "commit_hash": "", "dirty": True},
        headers={"X-Internal-Token": _TOKEN},
    )

    assert repo.updates[0][1]["last_applied_commit"] is None


async def test_unknown_environment_is_reported_not_raised(wired):
    client, repo = wired
    repo.update_result = False

    response = await client.post(
        "/internal/env-registry/applied",
        json={"environment_id": "gone", "commit_hash": "x"},
        headers={"X-Internal-Token": _TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"updated": False}


async def test_events_are_appended_whole(wired):
    client, repo = wired

    response = await client.post(
        "/internal/env-registry/events",
        json={
            "environment_id": "ws-t1-shop:prod",
            "action": "merge",
            "actor_user_id": "u1",
            "succeeded": False,
            "commit_hash": "deadbeef",
            "detail": {"from": "dev"},
        },
        headers={"X-Internal-Token": _TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"event_id": "ev-1"}
    assert repo.events[0]["action"] == "merge"
    assert repo.events[0]["succeeded"] is False
    assert repo.events[0]["detail"] == {"from": "dev"}


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/internal/env-registry/applied", {"environment_id": "e"}),
        ("/internal/env-registry/events", {"environment_id": "e", "action": "merge"}),
    ],
)
async def test_the_surface_fails_closed(wired, path, payload):
    client, repo = wired

    missing = await client.post(path, json=payload)
    wrong = await client.post(path, json=payload, headers={"X-Internal-Token": "nope"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert repo.updates == [] and repo.events == []


async def test_an_unset_token_rejects_everyone(wired, monkeypatch):
    """Unconfigured must mean "nobody", never "anybody"."""
    client, repo = wired
    monkeypatch.setattr(settings, "runtime_internal_token", "")

    response = await client.post(
        "/internal/env-registry/applied",
        json={"environment_id": "e"},
        headers={"X-Internal-Token": _TOKEN},
    )

    assert response.status_code == 401
    assert repo.updates == []


async def test_no_generic_column_update_is_exposed(wired):
    """The runtime may state a FACT, never name a column to write.

    A pass-through would let this channel bypass the registry's own
    whitelist — the invariant the control plane exists to hold.
    """
    client, repo = wired

    response = await client.post(
        "/internal/env-registry/applied",
        json={"environment_id": "e", "status": "archived", "name": "hacked"},
        headers={"X-Internal-Token": _TOKEN},
    )

    assert response.status_code == 200
    _environment_id, values = repo.updates[0]
    assert set(values) == {
        "last_applied_commit",
        "last_applied_at",
        "last_applied_dirty",
    }
