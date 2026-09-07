"""Lease placement and acquire-server tests (design 03 §3.3, design 07 Q3).

What needs pinning here is not that placement picks a number, but the DECISION
QUALITY the single-writer invariant depends on:

* a draining or full instance must never be chosen — placement is the only
  thing standing between a gateway miss and an instance that cannot serve;
* a refuser must be excluded on retry, because placement is deterministic and
  would otherwise pick it again;
* the accept handshake must commit the lease BEFORE answering, or a race would
  let the placer report success for a lease nobody took;
* admission must be enforced locally, since placement reads heartbeats a full
  interval stale.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.lease import (
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.lease.holder import LeaseHolder, LeaseUnavailable  # noqa: E402
from src.lease.manager import LeaseFullError, LeaseManager, RepositorySource  # noqa: E402
from src.lease.placement import NoInstanceAvailable, pick_instance  # noqa: E402
from src.lease.server import LeaseAcquireError, LeaseServer  # noqa: E402

_KEY = "t1/ws-t1-shop/u1/dev"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for lease server tests"
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, check=check, capture_output=True, text=True,
    )


class FakeAsyncRedis:
    """Async client honouring the operations placement performs."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self._expiry: dict[str, float] = {}

    async def set(self, key, value, nx=False, px=None, ex=None):
        if nx and key in self._values:
            return None
        self._values[key] = value
        if px is not None:
            self._expiry[key] = time.monotonic() + px / 1000
        elif ex is not None:
            self._expiry[key] = time.monotonic() + ex
        return True

    async def get(self, key):
        if key in self._expiry and time.monotonic() > self._expiry[key]:
            self._values.pop(key, None)
            self._expiry.pop(key, None)
        return self._values.get(key)

    async def delete(self, key):
        return 1 if self._values.pop(key, None) is not None else 0

    async def sadd(self, key, member):
        self._sets.setdefault(key, set()).add(member)
        return 1

    async def srem(self, key, member):
        self._sets.get(key, set()).discard(member)
        return 1

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def eval(self, script, _numkeys, key, *args):
        stored = self._values.get(key)
        owns = bool(stored) and json.loads(stored).get("token") == args[0]
        if "DEL" in script:
            if owns:
                del self._values[key]
                self._expiry.pop(key, None)
                return 1
            return 0
        if "PEXPIRE" in script:
            if owns:
                self._expiry[key] = time.monotonic() + int(args[1]) / 1000
                return 1
            return 0
        raise AssertionError(f"unexpected script: {script}")

    def publish_heartbeat(
        self, runtime_id: str, addr: str, lease_count: int,
        memory: float = 0.2, draining: bool = False, max_leases: int = 16,
    ) -> None:
        self._values[runtime_redis_key(runtime_id)] = json.dumps({
            "runtime_id": runtime_id, "addr": addr, "lease_count": lease_count,
            "max_leases": max_leases, "memory_used_ratio": memory,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "draining": draining, "leases": [],
        })
        self._sets.setdefault(runtime_index_redis_key(), set()).add(runtime_id)


@pytest.fixture()
def remote(tmp_path: Path) -> str:
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "model.sql").write_text("SELECT 1;\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    _git(work, "branch", "env/dev")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(work), str(bare))
    return str(bare)


@pytest.fixture()
def root(tmp_path: Path) -> str:
    path = tmp_path / "workcopies"
    path.mkdir()
    return str(path)


async def _source(repo_url: str) -> RepositorySource:
    return RepositorySource(repo_url=repo_url)


def _manager(client, root, remote, **overrides) -> LeaseManager:
    holder = LeaseHolder(
        client=client, materialize_root=root,
        runtime_addr="10.0.0.1:8002", ttl_seconds=120,
    )
    kwargs = {
        "holder": holder, "materialize_root": root,
        "source_provider": lambda k: _source(remote),
        "wip_interval_seconds": 3600,
    }
    kwargs.update(overrides)
    return LeaseManager(**kwargs)


class TestPlacement:
    async def test_picks_the_emptiest_instance(self):
        """The decision that keeps one instance from accumulating everything."""
        client = FakeAsyncRedis()
        client.publish_heartbeat("rt-a", "10.0.0.1:8002", lease_count=5)
        client.publish_heartbeat("rt-b", "10.0.0.2:8002", lease_count=1)

        placement = await pick_instance(client)
        assert placement.runtime_id == "rt-b"

    async def test_memory_breaks_count_ties(self):
        # The real cost of a lease scales with the USER'S repository size, not
        # with the count; a count tie must fall to the instance with headroom.
        client = FakeAsyncRedis()
        client.publish_heartbeat("rt-a", "10.0.0.1:8002", lease_count=2, memory=0.7)
        client.publish_heartbeat("rt-b", "10.0.0.2:8002", lease_count=2, memory=0.3)

        placement = await pick_instance(client)
        assert placement.runtime_id == "rt-b"

    async def test_over_watermark_is_skipped_not_deprioritized(self):
        # Sorting them lower would still place on them when they alone remain.
        client = FakeAsyncRedis()
        client.publish_heartbeat("rt-hot", "10.0.0.1:8002", lease_count=0, memory=0.95)

        with pytest.raises(NoInstanceAvailable):
            await pick_instance(client)

    async def test_draining_is_never_chosen(self):
        client = FakeAsyncRedis()
        client.publish_heartbeat("rt-drain", "10.0.0.1:8002", lease_count=0, draining=True)
        client.publish_heartbeat("rt-live", "10.0.0.2:8002", lease_count=9)

        placement = await pick_instance(client)
        assert placement.runtime_id == "rt-live"

    async def test_expired_heartbeat_member_is_skipped(self):
        # The index is a set and cannot expire per-member: a crashed instance's
        # id lingers, and choosing it would route every request to a dead pod.
        client = FakeAsyncRedis()
        client._sets[runtime_index_redis_key()] = {"rt-dead"}
        client.publish_heartbeat("rt-live", "10.0.0.2:8002", lease_count=1)

        placement = await pick_instance(client)
        assert placement.runtime_id == "rt-live"

    async def test_undecodable_heartbeat_is_skipped(self):
        client = FakeAsyncRedis()
        client._values[runtime_redis_key("rt-junk")] = "not json"
        client._sets[runtime_index_redis_key()] = {"rt-junk"}
        client.publish_heartbeat("rt-live", "10.0.0.2:8002", lease_count=1)

        assert (await pick_instance(client)).runtime_id == "rt-live"

    async def test_empty_index_is_unavailable(self):
        with pytest.raises(NoInstanceAvailable):
            await pick_instance(FakeAsyncRedis())

    async def test_exclude_drives_retries(self):
        # Without exclusion, a deterministic re-pick would hammer the refuser.
        client = FakeAsyncRedis()
        client.publish_heartbeat("rt-a", "10.0.0.1:8002", lease_count=0)
        client.publish_heartbeat("rt-b", "10.0.0.2:8002", lease_count=3)

        first = await pick_instance(client)
        assert first.runtime_id == "rt-a"
        retry = await pick_instance(client, exclude={first.runtime_id})
        assert retry.runtime_id == "rt-b"


class TestAcquireEndToEnd:
    async def test_acquisition_places_and_adopts_locally(
        self, client_factory, root, remote, monkeypatch
    ):
        """The gateway-facing happy path: miss → acquire → routable address."""
        client = client_factory()
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        result = await server.acquire(_KEY)

        assert result["runtime_id"] == "rt-self"
        assert result["addr"] == "10.0.0.1:8002"
        # The lease must be committed before "accepted" was answered.
        from datacode_common.lease import lease_redis_key
        assert await client.get(lease_redis_key(_KEY)) is not None

    async def test_adoption_returns_before_materialization_completes(
        self, client_factory, root, remote, monkeypatch
    ):
        """The design's `202 {warmup: in_progress}` behaviour.

        A large repository must not block the gateway request that triggered
        placement — the first forwarded request pays the cost instead.
        """
        client = client_factory()
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")

        gate = asyncio.Event()
        manager = _manager(client, root, remote)
        real = manager._materialize_and_protect

        async def slow(lease_key):
            await gate.wait()
            return await real(lease_key)

        manager._materialize_and_protect = slow
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )

        try:
            await asyncio.wait_for(server.acquire(_KEY), timeout=2.0)
        finally:
            gate.set()
        # acquire returned while materialization was still blocked: adopt
        # committed only the lease handshake.

    async def test_forwarded_request_waits_for_inflight_adoption(
        self, client_factory, root, remote, monkeypatch
    ):
        """The lock is the correctness mechanism: two materializations of one
        copy cannot race, and the forwarded request sees the adopted copy."""
        client = client_factory()
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        await server.acquire(_KEY)

        served = await manager.serve(_KEY)
        assert Path(served.workspace_path, "model.sql").exists()

    async def test_refused_winner_leads_to_replacement(
        self, client_factory, root, remote, monkeypatch
    ):
        """The stale-heartbeat case: winner refuses, placer must exclude it.

        Placement is deterministic, so without exclusion the retry would pick
        the same instance again and the acquire would fail for a workspace
        that another instance could serve.
        """
        client = client_factory()
        # rt-full looks emptiest (count 0) but will refuse: simulate a refusal
        # at the delegate boundary, which is exactly where a real full or
        # raced instance answers 409.
        client.publish_heartbeat("rt-full", "10.0.0.9:8002", lease_count=0)
        client.publish_heartbeat("rt-ok", "10.0.0.2:8002", lease_count=3)
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-ok")

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.2:8002",
        )

        calls: list[str] = []
        real_delegate = server._delegate

        async def delegate(placement, lease_key):
            calls.append(placement.runtime_id)
            if placement.runtime_id == "rt-full":
                # A real rt-full answers 409: raced or at capacity.
                raise LeaseUnavailable("rt-full: stale heartbeat said it was empty")
            return await real_delegate(placement, lease_key)

        server._delegate = delegate
        result = await server.acquire(_KEY)

        assert calls == ["rt-full", "rt-ok"], "retry must exclude the refuser"
        assert result["runtime_id"] == "rt-ok"

    async def test_no_capacity_anywhere_is_an_error(self, client_factory, root, monkeypatch):
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        manager = _manager(client, root, "http://unused")
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        with pytest.raises(LeaseAcquireError):
            await server.acquire(_KEY)


@pytest.fixture()
def client_factory():
    def factory() -> FakeAsyncRedis:
        return FakeAsyncRedis()
    return factory


def _publish_heartbeat_with_leases(
    client: FakeAsyncRedis, runtime_id: str, leases: list[str], **kwargs
) -> None:
    """A heartbeat that reports held lease keys (the quota guard's input)."""
    client.publish_heartbeat(
        runtime_id,
        f"10.0.{runtime_id[-1]}.1:8002",
        lease_count=len(leases),
        **kwargs,
    )
    raw = json.loads(client._values[runtime_redis_key(runtime_id)])
    raw["leases"] = leases
    client._values[runtime_redis_key(runtime_id)] = json.dumps(raw)


def _publish_quota(client: FakeAsyncRedis, payload: dict | None, *, tenant: str = "t1") -> None:
    """Write a quota snapshot exactly the way control-service publishes it."""
    from datetime import datetime, timezone

    from datacode_common.lease import encode_quota_snapshot, tenant_quota_redis_key

    snapshot = {
        "max_concurrent_leases": payload.get("leases"),
        "max_runtime_replicas": payload.get("replicas"),
        "updated_at": datetime.now(timezone.utc),
    }
    from datacode_common.lease import TenantQuotaSnapshot
    client._values[tenant_quota_redis_key(tenant)] = encode_quota_snapshot(
        TenantQuotaSnapshot(**snapshot)
    )


class TestTenantQuota:
    """P8: the acquire path answers for the tenant's capacity before placing."""

    async def test_at_lease_ceiling_is_a_429_with_retry_after(
        self, client_factory, root, remote, monkeypatch
    ):
        from src.utils.errors import QuotaExceededError

        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        # Another instance already holds the tenant's single allowed lease.
        _publish_heartbeat_with_leases(client, "rt-hold", ["t1/ws-old/u1/dev"])
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        _publish_quota(client, {"leases": 1})

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        with pytest.raises(QuotaExceededError) as exc_info:
            await server.acquire(_KEY)

        assert exc_info.value.status_code == 429
        assert "Retry-After" in (exc_info.value.headers or {})
        assert exc_info.value.detail["quota_resource"] == "concurrent lease"
        assert exc_info.value.detail["quota_current"] == 1

    async def test_reacquiring_a_held_lease_is_not_growth(
        self, client_factory, root, remote, monkeypatch
    ):
        """A lease a live instance still reports REPLACES itself, not +1.

        Without this rule the count would drift upward on every Redis-record
        expiry race, gradually locking the tenant below its real ceiling.
        """
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        _publish_heartbeat_with_leases(client, "rt-hold", [_KEY])
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        _publish_quota(client, {"leases": 1})

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        result = await server.acquire(_KEY)
        assert result["runtime_id"] == "rt-self"

    async def test_nothing_published_means_enforcement_off(
        self, client_factory, root, remote, monkeypatch
    ):
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        # Held leases exist but no quota key does: place as before.
        _publish_heartbeat_with_leases(client, "rt-hold", ["t1/ws-old/u1/dev"])
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        result = await server.acquire(_KEY)
        assert result["runtime_id"] == "rt-self"

    async def test_defaults_key_applies_when_the_tenant_has_no_row(
        self, client_factory, root, remote, monkeypatch
    ):
        from src.utils.errors import QuotaExceededError

        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        _publish_heartbeat_with_leases(
            client, "rt-hold", ["t1/ws-old/u1/dev", "t1/ws-old/u2/dev"]
        )
        client.publish_heartbeat("rt-self", "10.0.0.1:8002", lease_count=0)
        # Only the platform defaults key exists (the no-row tenant case).
        from datetime import datetime, timezone

        from datacode_common.lease import (
            TenantQuotaSnapshot,
            encode_quota_snapshot,
            quota_defaults_redis_key,
        )

        client._values[quota_defaults_redis_key()] = encode_quota_snapshot(
            TenantQuotaSnapshot(
                max_concurrent_leases=2,
                max_runtime_replicas=None,
                updated_at=datetime.now(timezone.utc),
            )
        )

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        with pytest.raises(QuotaExceededError):
            await server.acquire(_KEY)

    async def test_replica_cap_consolidates_onto_the_existing_footprint(
        self, client_factory, root, remote, monkeypatch
    ):
        """At the replica cap, placement must prefer the tenant's instances.

        rt-empty is the emptiest instance overall, but steering excludes it so
        the new lease lands on rt-tenant — spreading the footprint is exactly
        what the cap forbids.
        """
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-tenant")
        _publish_heartbeat_with_leases(client, "rt-tenant", ["t1/ws-old/u1/dev"])
        client.publish_heartbeat("rt-empty", "10.0.0.2:8002", lease_count=0)
        _publish_quota(client, {"leases": None, "replicas": 1})

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        result = await server.acquire(_KEY)
        assert result["runtime_id"] == "rt-tenant"

    async def test_full_footprint_is_a_quota_rejection(
        self, client_factory, root, remote, monkeypatch
    ):
        from src.utils.errors import QuotaExceededError

        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        _publish_heartbeat_with_leases(client, "rt-tenant", ["t1/ws-old/u1/dev"])
        client.publish_heartbeat("rt-empty", "10.0.0.2:8002", lease_count=0)
        _publish_quota(client, {"leases": None, "replicas": 1})

        manager = _manager(client, root, remote)
        server = LeaseServer(
            manager=manager, client=client,
            internal_token="tok", runtime_addr="10.0.0.1:8002",
        )
        # The one tenant-occupied instance refuses (stale heartbeat said it
        # was empty): the footprint cannot grow, which is a quota verdict.
        real_delegate = server._delegate

        async def refusing_delegate(placement, lease_key):
            if placement.runtime_id == "rt-tenant":
                raise LeaseUnavailable("rt-tenant: at capacity")
            return await real_delegate(placement, lease_key)

        server._delegate = refusing_delegate
        with pytest.raises(QuotaExceededError) as exc_info:
            await server.acquire(_KEY)
        assert exc_info.value.detail["quota_resource"] == "runtime replica"


class TestLocalAdmission:
    async def test_count_limit_is_enforced_locally(self, client_factory, root, remote, monkeypatch):
        """Q3's enforcement point: placement reads stale data, the instance
        does not. Two concurrent placements must not both squeeze in."""
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        manager = _manager(client, root, remote, max_leases=1)

        served = await manager.serve(_KEY)
        assert served.freshly_materialized is True

        other = "t1/ws-t1-shop/u2/dev"
        with pytest.raises(LeaseFullError):
            await manager.serve(other)

    async def test_memory_watermark_is_enforced_locally(
        self, client_factory, root, remote, monkeypatch
    ):
        # A wrong per-lease estimate must surface as a refused lease, not an
        # OOM that kills every OTHER lease on the instance too.
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        manager = _manager(
            client, root, remote,
            max_leases=100,
            admission_high_watermark=0.5,
            memory_ratio_provider=lambda: 0.9,
        )
        with pytest.raises(LeaseFullError, match="memory"):
            await manager.serve(_KEY)

    async def test_admission_failure_leaves_no_lease_behind(
        self, client_factory, root, remote, monkeypatch
    ):
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        manager = _manager(
            client, root, remote,
            max_leases=100,
            memory_ratio_provider=lambda: 0.9,
        )
        from datacode_common.lease import lease_redis_key

        with pytest.raises(LeaseFullError):
            await manager.serve(_KEY)
        assert await client.get(lease_redis_key(_KEY)) is None

    async def test_zero_max_leases_means_memory_only(
        self, client_factory, root, remote, monkeypatch
    ):
        # `max_per_instance: 0` documents "derive capacity from the memory
        # budget alone"; treating it as "capacity zero" would idle the pod.
        client = client_factory()
        monkeypatch.setenv("DATACODE_RUNTIME_ID", "rt-self")
        manager = _manager(
            client, root, remote,
            max_leases=0,
            memory_ratio_provider=lambda: 0.1,
        )
        for i in range(3):
            await manager.serve(f"t1/ws-t1-shop/u{i}/dev")
        assert len(manager.served_keys()) == 3
