"""Lease-gate tests for the request path (design 08 §7, LEASE_NOT_HELD).

The gate lives in ``validate_workspace_path`` — the single dependency every
gateway-facing working-copy route funnels through. These tests pin the three
outcomes the contract needs:

* lease subsystem off → the gate is silent (shared-medium deployment);
* this replica holds the lease → the request proceeds;
* it does not → 409 LEASE_NOT_HELD, the code the gateway re-routes on.

The last one is the whole point: without a producer for that code, the
gateway's stale-route retry had no trigger, and a request that landed on the
wrong replica would read a copy that volume may not even contain.
"""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest

from src.api.deps import validate_workspace_path
from src.utils.errors import ErrorCode, ServiceError


class _FakeManager:
    """Just what the gate reads: held keys and the draining flag."""

    def __init__(self, held: t.Set[str], *, draining: bool = False) -> None:
        self._held = set(held)
        self.draining = draining

    def holds(self, lease_key: str) -> bool:
        return lease_key in self._held


def _working_copy(base: Path) -> str:
    """Materialize a path with the layout parse_workspace_path demands.

    The workspace id must carry the ``ws{tenant}-`` prefix — ownership of a
    workspace directory is encoded in the path itself.
    """
    copy = base / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev_alice"
    copy.mkdir(parents=True)
    return str(copy)


def _with_manager(monkeypatch, manager: t.Optional[_FakeManager]) -> None:
    import src.lease.wiring as wiring

    monkeypatch.setattr(wiring, "get_lease_manager", lambda: manager)


@pytest.fixture()
def workspace_base(tmp_path, monkeypatch):
    """A configured base with one valid working copy inside it."""
    from src.config import settings

    monkeypatch.setattr(settings, "workspace_base_path", str(tmp_path))
    yield tmp_path


async def test_gate_is_silent_when_the_lease_subsystem_is_off(
    workspace_base, monkeypatch
):
    """No manager = shared-medium deployment: pre-P4a behaviour is preserved.

    The storage contract already refuses the unsafe combination
    (instance-local medium + leases off) at startup, so the gate must not
    add a second refusal here.
    """
    _with_manager(monkeypatch, None)
    path = _working_copy(workspace_base)

    assert validate_workspace_path(path) == str(Path(path).resolve())


async def test_holding_replica_serves_the_request(workspace_base, monkeypatch):
    """The holder is exactly who the gateway's route points at."""
    _with_manager(monkeypatch, _FakeManager({"t1/ws-t1-shop/u1/dev_alice"}))
    path = _working_copy(workspace_base)

    assert validate_workspace_path(path) == str(Path(path).resolve())


async def test_non_holder_answers_lease_not_held(workspace_base, monkeypatch):
    """A request for a copy this replica does not own is a routing verdict.

    The gateway's route cache has a TTL and a lease can move inside that
    window; serving the request anyway would read whatever this volume
    happens to have — stale on shared media, absent on instance-local.
    """
    _with_manager(monkeypatch, _FakeManager({"t1/ws-t1-shop/someone-else/dev_bob"}))
    path = _working_copy(workspace_base)

    with pytest.raises(ServiceError) as excinfo:
        validate_workspace_path(path)

    assert excinfo.value.code == ErrorCode.LEASE_NOT_HELD
    assert excinfo.value.status_code == 409
    assert excinfo.value.to_response_dict()["error_code"] == "LEASE_NOT_HELD"


async def test_draining_non_holder_answers_relocating(workspace_base, monkeypatch):
    """503 WORKSPACE_RELOCATING: the copy is MOVING, not merely elsewhere.

    A draining instance just released it, so the truthful answer is "wait out
    the hand-over" — the verdict the frontend interceptor keys its banner and
    transparent retry on (design 03 §4.3). Answering LEASE_NOT_HELD here
    would also work, but it hides the one fact the user cares about: this is
    a planned move, not a failure.
    """
    _with_manager(
        monkeypatch,
        _FakeManager({"t1/ws-t1-shop/someone-else/dev_bob"}, draining=True),
    )
    path = _working_copy(workspace_base)

    with pytest.raises(ServiceError) as excinfo:
        validate_workspace_path(path)

    assert excinfo.value.code == ErrorCode.WORKSPACE_RELOCATING
    assert excinfo.value.status_code == 503
    assert excinfo.value.headers == {"Retry-After": "5"}


async def test_lease_key_is_composed_from_the_parsed_path(
    workspace_base, monkeypatch
):
    """The gate must judge the lease the PATH identifies, not a guess.

    A user_id/environment mixup would pass someone else's copy while
    rejecting the requester's own.
    """
    seen: t.List[str] = []

    class _RecordingManager(_FakeManager):
        def holds(self, lease_key: str) -> bool:
            seen.append(lease_key)
            return True

    _with_manager(monkeypatch, _RecordingManager(set()))
    path = _working_copy(workspace_base)

    validate_workspace_path(path)

    assert seen == ["t1/ws-t1-shop/u1/dev_alice"]


async def test_held_copy_not_yet_on_disk_answers_materializing(
    workspace_base, monkeypatch
):
    """202 WORKSPACE_MATERIALIZING: the first requests after a placement race
    the background clone (adopt() commits the lease first, materializes after).

    The invariant that makes the verdict safe: a materialization failure
    RELEASES the lease, so "held but missing" can only mean "still
    materializing" — an honest "retry shortly", not an error. The old answer
    here was a misleading 400 "path does not exist".
    """
    _with_manager(monkeypatch, _FakeManager({"t1/ws-t1-shop/u1/dev_alice"}))
    # The working copy directory deliberately does NOT exist: the clone is
    # still running.
    pending = workspace_base / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev_alice"
    pending.parent.mkdir(parents=True)

    with pytest.raises(ServiceError) as excinfo:
        validate_workspace_path(str(pending))

    assert excinfo.value.code == ErrorCode.WORKSPACE_MATERIALIZING
    assert excinfo.value.status_code == 202
    assert excinfo.value.headers == {"Retry-After": "2"}


async def test_missing_path_without_a_lease_subsystem_is_still_a_client_error(
    workspace_base, monkeypatch
):
    """Leases off: a missing path is the caller's mistake (400), not a
    materialization state — the 202 verdict is reserved for copies this
    replica is known to be producing."""
    _with_manager(monkeypatch, None)
    missing = workspace_base / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev_alice"
    missing.parent.mkdir(parents=True)

    with pytest.raises(ServiceError) as excinfo:
        validate_workspace_path(str(missing))

    assert excinfo.value.code == ErrorCode.WORKSPACE_PATH_INVALID
    assert excinfo.value.status_code == 400


async def test_invalid_layout_is_rejected_before_the_lease_lookup(
    workspace_base, monkeypatch
):
    """Layout validation precedes ownership: no lease key can be composed
    from a malformed path, so the gate must never even be consulted."""
    called = []

    class _ExplodingManager(_FakeManager):
        def holds(self, lease_key: str) -> bool:  # pragma: no cover
            called.append(lease_key)
            return True

    _with_manager(monkeypatch, _ExplodingManager(set()))
    _working_copy(workspace_base)
    bad = workspace_base / "t1" / "not-a-layout"

    with pytest.raises(ServiceError) as excinfo:
        validate_workspace_path(str(bad))

    assert excinfo.value.code == ErrorCode.WORKSPACE_PATH_INVALID
    assert called == []
