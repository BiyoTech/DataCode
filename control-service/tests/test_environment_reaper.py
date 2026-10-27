"""Tests for sandbox reclamation (environments.reaper).

The reaper is the only thing keeping environment resources bounded, and it
destroys data, so both halves of its contract are pinned here:

* an expired sandbox is ANNOUNCED first and survives its grace period;
* past the grace period it is reclaimed exactly once, and a busy environment is
  left alone instead of being interrupted.

The registry and the repository are substituted: this is the reaper's decision
logic, not an integration test of the runtime or the warehouse.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from datacode_common import naming

from src.config import settings
from src.environments import reaper as reaper_module
from src.environments.reaper import EnvironmentReaper
from src.utils.errors import EnvironmentBusyError

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Env:
    """Minimal stand-in for an environment row."""

    id: str
    tenant_id: str = "t1"
    workspace_id: str = "ws-t1-sales"
    owner_user_id: str = "alice"
    ttl_hours: int = 1
    expires_at: t.Optional[datetime] = None


class _FakeRepo:
    """Records events in memory and answers the reaper's queries from them."""

    def __init__(self, expired: t.List[_Env]) -> None:
        self._expired = expired
        self.events: t.List[t.Dict[str, t.Any]] = []

    async def list_expired(self, now=None):
        return list(self._expired)

    async def count_events(self, environment_id, action, since=None):
        return sum(
            1
            for event in self.events
            if event["environment_id"] == environment_id
            and event["action"] == action
            and (since is None or event["at"] >= since)
        )

    async def record_event(self, environment_id, action, **kwargs):
        self.events.append(
            {
                "environment_id": environment_id,
                "action": action,
                "at": _NOW,
                **kwargs,
            }
        )
        return "event-id"


@dataclass
class _DestroyResult:
    archived: bool = True
    virtual_environment_released: bool = True
    worktrees_removed: int = 2


class _FakeRegistry:
    """Captures destroy calls; optionally reports the environment as busy."""

    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.calls: t.List[t.Dict[str, t.Any]] = []

    async def destroy(self, **kwargs):
        self.calls.append(kwargs)
        if self.busy:
            raise EnvironmentBusyError(kwargs["environment_id"], "apply")
        return _DestroyResult()


@pytest.fixture
def wire(monkeypatch):
    """Build a reaper wired to fakes, with a deterministic grace period."""

    def _wire(expired: t.List[_Env], busy: bool = False, grace_hours: int = 24):
        monkeypatch.setattr(settings, "sandbox_expiry_grace_hours", grace_hours)
        repo = _FakeRepo(expired)
        registry = _FakeRegistry(busy=busy)
        instance = EnvironmentReaper(
            repository=repo, registry_factory=lambda: registry
        )
        return instance, repo, registry

    return _wire


async def test_expiry_is_announced_before_anything_is_destroyed(wire):
    """Reaching expires_at must warn, never destroy: the grace period is the
    owner's chance to keep work that only exists in this environment."""
    env = _Env(id="ws-t1-sales:dev_alice", expires_at=_NOW - timedelta(hours=1))
    instance, repo, registry = wire([env])

    outcome = await instance.run_once(now=_NOW)

    assert (outcome.warned, outcome.reclaimed) == (1, 0)
    assert registry.calls == []
    assert repo.events[0]["action"] == naming.ACTION_EXPIRE
    assert repo.events[0]["detail"]["stage"] == "warned"


async def test_warning_is_recorded_only_once_per_expiry(wire):
    """A scan runs hourly; it must not re-announce the same expiry every tick."""
    env = _Env(id="ws-t1-sales:dev_alice", expires_at=_NOW - timedelta(hours=1))
    instance, repo, _ = wire([env])

    first = await instance.run_once(now=_NOW)
    second = await instance.run_once(now=_NOW)

    assert (first.warned, second.warned) == (1, 0)
    assert len(repo.events) == 1


async def test_reclaimed_after_the_grace_period(wire):
    """Past the grace period the environment is destroyed — forced, because an
    abandoned sandbox is exactly the case where nobody resolves its changes."""
    env = _Env(id="ws-t1-sales:dev_alice", expires_at=_NOW - timedelta(hours=30))
    instance, repo, registry = wire([env], grace_hours=24)

    outcome = await instance.run_once(now=_NOW)

    assert (outcome.warned, outcome.reclaimed) == (0, 1)
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["environment_id"] == env.id
    assert call["force"] is True
    assert call["trigger_type"] == reaper_module.TRIGGER_SCHEDULE
    assert repo.events[-1]["detail"]["stage"] == "reclaimed"


async def test_busy_environment_is_left_for_the_next_tick(wire):
    """An apply in flight owns the environment: reclaiming would interrupt it."""
    env = _Env(id="ws-t1-sales:dev_alice", expires_at=_NOW - timedelta(hours=30))
    instance, repo, registry = wire([env], busy=True)

    outcome = await instance.run_once(now=_NOW)

    assert (outcome.reclaimed, outcome.skipped_busy, outcome.failed) == (0, 1, 0)
    assert not [e for e in repo.events if e["detail"].get("stage") == "reclaimed"]


async def test_one_failure_does_not_abort_the_scan(wire):
    """Per-row isolation: a single broken environment must not stop reclamation
    of every other expired sandbox in the platform."""
    first = _Env(id="ws-t1-sales:dev_a", expires_at=_NOW - timedelta(hours=30))
    second = _Env(id="ws-t1-sales:dev_b", expires_at=_NOW - timedelta(hours=30))
    instance, _, registry = wire([first, second])

    async def explode_on_first(**kwargs):
        registry.calls.append(kwargs)
        if kwargs["environment_id"] == first.id:
            raise RuntimeError("git exploded")
        return _DestroyResult()

    registry.destroy = explode_on_first  # type: ignore[method-assign]

    outcome = await instance.run_once(now=_NOW)

    assert (outcome.failed, outcome.reclaimed) == (1, 1)
    assert len(registry.calls) == 2


async def test_naive_timestamps_are_treated_as_utc(wire):
    """SQLite returns naive datetimes even for timezone-aware columns, and the
    platform stores UTC by contract, so comparison must not raise."""
    env = _Env(
        id="ws-t1-sales:dev_alice",
        expires_at=(_NOW - timedelta(hours=30)).replace(tzinfo=None),
    )
    instance, _, registry = wire([env])

    outcome = await instance.run_once(now=_NOW)

    assert outcome.reclaimed == 1
    assert len(registry.calls) == 1
