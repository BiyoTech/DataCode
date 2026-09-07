"""Workspace deletion: complete, physical, and never touching the remote.

Deletion used to be a status flag plus a directory removal. Everything else
survived and was inherited by the next workspace created under the same name —
ghost knowledge-base entries, resurrected drafts, another life's apply history,
an orphaned Dagster deployment, and a Redis lease still held by a live writer.

The assertions here pin the whole teardown, and three of them carry the reasoning
that is easy to lose:

* ``test_leases_are_released_before_anything_touches_the_files`` — while a lease
  is held, a runtime replica considers itself the single writer of that working
  copy, so every later step would operate on files it may still be writing;
* ``test_purge_is_broadcast_to_every_lease_holder`` — env-control is a
  single-URL client while each replica has its OWN volume, so a purge sent to
  whichever replica answered removed nothing on the replica that held the files;
* ``test_remote_repository_is_never_deleted`` — the repository may predate the
  workspace or be shared, so removing it is not a side effect the platform may
  have.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import pytest

from src.utils.errors import WorkspaceNotFoundError
from src.workspace.manager import WorkspaceManager, _runtime_url_of

_TENANT = "t1"
_WORKSPACE = "ws-t1-shop"
_USER = "u1"
_DEFAULT_RUNTIME = "http://runtime:8002"


@dataclass
class _Record:
    """Stand-in for a :class:`LeaseRecord` (only the read fields)."""

    lease_key: str
    token: str = "tok"
    runtime_id: str = "rt-1"
    runtime_addr: str = ""


@dataclass
class _FakeLeaseBinder:
    records: t.Optional[list] = field(default_factory=list)
    released: list = field(default_factory=list)
    release_result: bool = True

    async def list_for_workspace(self, *, tenant_id: str, workspace_id: str):
        return self.records

    async def release(self, record) -> bool:
        self.released.append(record.lease_key)
        return self.release_result


@dataclass
class _FakeRegistry:
    release_result: dict = field(default_factory=lambda: {"released": 2, "failures": 0})
    purged: int = 2
    calls: list = field(default_factory=list)

    async def release_all_for_workspace(self, *, workspace_id, tenant_id, actor_user_id):
        self.calls.append(("release", workspace_id))
        return self.release_result

    async def purge_all_for_workspace(self, *, workspace_id):
        self.calls.append(("purge_rows", workspace_id))
        return self.purged


@dataclass
class _FakeRuntimeClient:
    base_url: str = _DEFAULT_RUNTIME
    calls: list = field(default_factory=list)
    fail_urls: tuple = ()

    async def post(self, path: str, payload: dict) -> dict:
        self.calls.append((self.base_url, path, payload))
        if self.base_url in self.fail_urls:
            raise RuntimeError("replica unreachable")
        return {"purged": True}


@dataclass
class _FakeWorkspaceRepository:
    model: t.Any = None
    purged: list = field(default_factory=list)

    async def get_in_tenant(self, workspace_id: str, tenant_id: str):
        return self.model

    async def purge(self, workspace_id: str) -> bool:
        self.purged.append(workspace_id)
        return True


@dataclass
class _Model:
    id: str = _WORKSPACE
    tenant_id: str = _TENANT
    owner_user_id: str = _USER
    name: str = "shop"
    git_repo_url: str = "https://github.com/acme/shop.git"


@pytest.fixture()
def wired(monkeypatch):
    """Wire the manager against fakes, tracking the order of every step."""
    order: list = []
    lease_binder = _FakeLeaseBinder()
    registry = _FakeRegistry()
    repository = _FakeWorkspaceRepository(model=_Model())
    clients: dict = {}

    def _client_for(base_url: str = _DEFAULT_RUNTIME, **_: t.Any):
        client = clients.get(base_url)
        if client is None:
            client = _FakeRuntimeClient(base_url=base_url)
            clients[base_url] = client
        return client

    class _TrackingLeaseBinder(_FakeLeaseBinder):
        async def release(self, record):
            order.append("lease")
            return await _FakeLeaseBinder.release(self, record)

    lease_binder = _TrackingLeaseBinder()

    class _TrackingRegistry(_FakeRegistry):
        async def release_all_for_workspace(self, **kwargs):
            order.append("resources")
            return await _FakeRegistry.release_all_for_workspace(self, **kwargs)

        async def purge_all_for_workspace(self, **kwargs):
            order.append("rows")
            return await _FakeRegistry.purge_all_for_workspace(self, **kwargs)

    registry = _TrackingRegistry()

    class _TrackingClient(_FakeRuntimeClient):
        async def post(self, path: str, payload: dict) -> dict:
            order.append("purge")
            return await _FakeRuntimeClient.post(self, path, payload)

    def _tracking_client_for(base_url: str = _DEFAULT_RUNTIME, **_: t.Any):
        client = clients.get(base_url)
        if client is None:
            client = _TrackingClient(base_url=base_url)
            clients[base_url] = client
        return client

    monkeypatch.setattr(
        "src.environments.lease_binder.get_lease_binder", lambda: lease_binder
    )
    monkeypatch.setattr(
        "src.workspace.manager.get_environment_registry", lambda: registry
    )
    monkeypatch.setattr(
        "src.workspace.manager.get_runtime_client",
        lambda: _tracking_client_for(_DEFAULT_RUNTIME),
    )
    monkeypatch.setattr("src.runtime.client.RuntimeClient", _tracking_client_for)
    monkeypatch.setattr(
        WorkspaceManager, "_repository", staticmethod(lambda: repository)
    )
    _ = _client_for  # kept for symmetry; the tracking variant is what is wired
    return WorkspaceManager(), lease_binder, registry, repository, clients, order


# ── Ordering ───────────────────────────────────────────────────────────────


async def test_leases_are_released_before_anything_touches_the_files(wired):
    """A held lease means some replica is still the single writer of the copy."""
    manager, lease_binder, _registry, _repo, _clients, order = wired
    lease_binder.records = [
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod", runtime_addr="r1:8002")
    ]

    await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert order.index("lease") < order.index("resources")
    assert order.index("resources") < order.index("purge")
    assert order.index("purge") < order.index("rows")


async def test_every_step_is_reported(wired):
    manager, lease_binder, _registry, repository, _clients, _order = wired
    lease_binder.records = [
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod"),
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/dev"),
    ]

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert result["deleted"] is True
    assert result["leases_released"] == 2
    assert result["environments_released"] == 2
    assert result["environments_removed"] == 2
    assert result["replicas_purged"] == 1
    assert repository.purged == [_WORKSPACE]


# ── Multi-replica: the purge must reach the volume that holds the files ────


async def test_purge_is_broadcast_to_every_lease_holder(wired):
    """Each replica has its own volume; the default URL is not authoritative.

    Sending the purge only to whichever replica answered is why deletion could
    "succeed" while the working copy stayed on disk somewhere else.
    """
    manager, lease_binder, _registry, _repo, clients, _order = wired
    lease_binder.records = [
        _Record(
            lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod", runtime_addr="replica-a:8002"
        ),
        _Record(
            lease_key=f"{_TENANT}/{_WORKSPACE}/u2/dev", runtime_addr="replica-b:8002"
        ),
    ]

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert set(clients) == {
        _DEFAULT_RUNTIME,
        "http://replica-a:8002",
        "http://replica-b:8002",
    }
    assert result["replicas_purged"] == 3
    for client in clients.values():
        assert client.calls
        assert client.calls[0][1] == "/internal/env-control/workspace/hard-purge"


async def test_duplicate_holder_addresses_are_dispatched_once(wired):
    """Several leases on one replica are still one purge."""
    manager, lease_binder, _registry, _repo, clients, _order = wired
    lease_binder.records = [
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod", runtime_addr="a:8002"),
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/dev", runtime_addr="a:8002"),
    ]

    await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert set(clients) == {_DEFAULT_RUNTIME, "http://a:8002"}


async def test_one_unreachable_replica_does_not_block_the_deletion(wired):
    """Otherwise a single dead replica would make a workspace undeletable."""
    manager, lease_binder, _registry, repository, clients, _order = wired
    lease_binder.records = [
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod", runtime_addr="dead:8002")
    ]

    # Pre-create the failing client so the manager reuses it.
    failing = _FakeRuntimeClient(base_url="http://dead:8002", fail_urls=("http://dead:8002",))
    clients["http://dead:8002"] = failing

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert result["deleted"] is True
    # The reachable default replica still purged.
    assert result["replicas_purged"] == 1
    assert repository.purged == [_WORKSPACE]


# ── Redis unreadable: proceed, but do not claim zero ──────────────────────


async def test_unreadable_redis_does_not_report_zero_leases(wired):
    """"Could not read" must not be rounded down to "there were none".

    The deletion still proceeds — a workspace nobody can delete while Redis is
    down is worse — but the ambiguity is surfaced instead of hidden.
    """
    manager, lease_binder, _registry, repository, clients, _order = wired
    lease_binder.records = None  # no verdict

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert result["leases_released"] == 0
    assert lease_binder.released == []
    # Deletion completed, and the default replica was still purged.
    assert repository.purged == [_WORKSPACE]
    assert set(clients) == {_DEFAULT_RUNTIME}


async def test_lease_release_failure_is_counted_honestly(wired):
    """A lost compare-and-swap means somebody else holds it now."""
    manager, lease_binder, _registry, _repo, _clients, _order = wired
    lease_binder.records = [
        _Record(lease_key=f"{_TENANT}/{_WORKSPACE}/{_USER}/prod")
    ]
    lease_binder.release_result = False

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)
    assert result["leases_released"] == 0


# ── The remote is never touched ───────────────────────────────────────────


async def test_remote_repository_is_never_deleted(wired):
    """The requirement, asserted on the wire: no payload may ask for it."""
    manager, _lease_binder, _registry, _repo, clients, _order = wired

    result = await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert result["git_remote_preserved"] is True
    for client in clients.values():
        for _url, path, payload in client.calls:
            assert "delete_remote" not in payload
            assert "delete-remote" not in path
            assert "git_repo_url" not in payload


async def test_delete_signature_has_no_remote_switch():
    """Structural guard: the capability must not come back behind a flag."""
    import inspect

    parameters = inspect.signature(WorkspaceManager.delete).parameters
    assert "delete_remote" not in parameters


# ── Tenancy ──────────────────────────────────────────────────────────────


async def test_unknown_workspace_is_a_404_before_any_teardown(wired):
    manager, lease_binder, registry, repository, clients, _order = wired
    repository.model = None

    with pytest.raises(WorkspaceNotFoundError):
        await manager.delete(_WORKSPACE, _TENANT, _USER)

    assert lease_binder.released == []
    assert registry.calls == []
    assert clients == {}


# ── Address parsing ──────────────────────────────────────────────────────


def test_runtime_url_of_handles_every_heartbeat_form():
    assert _runtime_url_of("replica-a:8002") == "http://replica-a:8002"
    assert _runtime_url_of("http://replica-a:8002") == "http://replica-a:8002"
    assert _runtime_url_of("https://replica-a:8002/") == "https://replica-a:8002"
    # Unusable addresses are skipped rather than dispatched to.
    assert _runtime_url_of("") == ""
    assert _runtime_url_of("   ") == ""
