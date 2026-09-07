"""Egress grant store tests.

The store is the channel that tells the proxy what a task may reach, so the
properties under test are about failure direction: a lost grant, an unreachable
store and a corrupt entry must all end in a denial, never in an allow.
"""

from __future__ import annotations

import time

import pytest

from datacode_common.egress import EgressDestination
from datacode_common.egress_grant import (
    DEFAULT_BYTE_QUOTA,
    GRANT_TTL_MARGIN_SECONDS,
    EgressGrant,
    EgressGrantStore,
    EgressGrantUnavailable,
    grant_key,
    usage_key,
    warehouse_destinations,
)

TASK = "task0001"
TENANT = "tenant-1"


async def _no_client():
    """A store with no shared backend.

    Injected explicitly rather than relying on Redis being absent: a suite running
    next to a live Redis would otherwise exercise the wrong branch and report
    success for behaviour it never touched.
    """
    return None


def _grant(**overrides) -> EgressGrant:
    base = {
        "task_id": TASK,
        "tenant_id": TENANT,
        "workspace_id": "ws-1",
        "isolation_profile": "L2_container",
        "destinations": (
            EgressDestination(host="wh.acme.example", ports=(5432,), kind="warehouse"),
        ),
        "denied_cidrs": ("203.0.113.0/24",),
        "expires_at": int(time.time()) + 600,
    }
    base.update(overrides)
    return EgressGrant(**base)


class TestSerialization:
    def test_round_trip_preserves_every_field(self):
        grant = _grant()

        restored = EgressGrant.from_json(grant.to_json())

        assert restored == grant

    def test_denied_cidrs_survive(self):
        """They only reach the proxy through the grant, so losing them is silent."""
        restored = EgressGrant.from_json(_grant().to_json())

        assert restored.denied_cidrs == ("203.0.113.0/24",)

    def test_grant_carries_no_credentials(self):
        """It lists destinations, never how to authenticate to them."""
        payload = _grant().to_json().lower()

        for marker in ("password", "token", "secret", "credential"):
            assert marker not in payload

    def test_malformed_destination_entries_are_dropped_not_fatal(self):
        restored = EgressGrant.from_dict(
            {
                "task_id": TASK,
                "tenant_id": TENANT,
                "destinations": [{"host": ""}, "not-a-mapping", {"host": "ok.example"}],
            }
        )

        assert [d.host for d in restored.destinations] == ["ok.example"]


class TestQuotaDefaults:
    def test_zero_means_the_default_not_unlimited(self):
        """An unlimited default is how exfiltration goes unnoticed."""
        assert _grant(byte_quota=0).effective_quota == DEFAULT_BYTE_QUOTA

    def test_explicit_quota_wins(self):
        assert _grant(byte_quota=1024).effective_quota == 1024


class TestExpiry:
    def test_expired_grant_reports_itself(self):
        assert _grant(expires_at=int(time.time()) - 1).is_expired()

    def test_live_grant_is_not_expired(self):
        assert not _grant().is_expired()


class TestLocalFallback:
    """The in-process store, used only by single-process dev and tests."""

    async def test_publish_then_fetch(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)

        await store.publish(_grant(), ttl_seconds=60)
        fetched = await store.fetch(TASK)

        assert fetched is not None
        assert fetched.tenant_id == TENANT

    async def test_fetch_of_an_unknown_task_returns_none(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)

        assert await store.fetch("nope") is None

    async def test_revoke_removes_the_grant(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)
        await store.publish(_grant(), ttl_seconds=60)

        await store.revoke(TASK)

        assert await store.fetch(TASK) is None

    async def test_usage_accumulates(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)

        first = await store.add_usage(TASK, 100, ttl_seconds=60)
        second = await store.add_usage(TASK, 50, ttl_seconds=60)

        assert (first, second) == (100, 150)
        assert await store.usage(TASK) == 150

    async def test_non_positive_usage_is_ignored(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)

        assert await store.add_usage(TASK, 0, ttl_seconds=60) == 0
        assert await store.usage(TASK) == 0


class TestFailClosed:
    """Without a shared store, the platform must refuse rather than diverge."""

    async def test_publish_without_a_store_raises(self):
        store = EgressGrantStore(allow_local_fallback=False, client_factory=_no_client)

        with pytest.raises(EgressGrantUnavailable, match="no shared grant store"):
            await store.publish(_grant(), ttl_seconds=60)

    async def test_fetch_without_a_store_raises_rather_than_returning_none(self):
        """A distinct outcome from 'no grant': one is an outage, one a denial."""
        store = EgressGrantStore(allow_local_fallback=False, client_factory=_no_client)

        with pytest.raises(EgressGrantUnavailable, match="unreachable"):
            await store.fetch(TASK)

    async def test_corrupt_entry_denies_without_looking_like_an_outage(self):
        store = EgressGrantStore(allow_local_fallback=True, client_factory=_no_client)
        store._local[grant_key(TASK)] = "{not json"

        assert await store.fetch(TASK) is None


class FakeRedis:
    """The subset of the Redis API this store uses."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self._maybe_fail()
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key):
        self._maybe_fail()
        return self.values.get(key)

    async def delete(self, *keys):
        self._maybe_fail()
        for key in keys:
            self.values.pop(key, None)

    async def incrby(self, key, amount):
        self._maybe_fail()
        total = int(self.values.get(key, 0)) + amount
        self.values[key] = str(total)
        return total

    async def expire(self, key, ttl):
        self._maybe_fail()
        self.ttls[key] = ttl

    def _maybe_fail(self):
        if self.failing:
            raise RuntimeError("redis is down")


class TestSharedStore:
    """The production path: a shared backend both sides can read."""

    def _store(self, client: FakeRedis) -> EgressGrantStore:
        async def factory():
            return client

        return EgressGrantStore(allow_local_fallback=False, client_factory=factory)

    async def test_publish_sets_a_ttl_beyond_the_task(self):
        client = FakeRedis()

        await self._store(client).publish(_grant(), ttl_seconds=300)

        assert client.ttls[grant_key(TASK)] == 300 + GRANT_TTL_MARGIN_SECONDS

    async def test_published_grant_is_readable_by_another_store(self):
        """Writer and reader are different processes in production."""
        client = FakeRedis()
        await self._store(client).publish(_grant(), ttl_seconds=300)

        fetched = await self._store(client).fetch(TASK)

        assert fetched is not None
        assert fetched.denied_cidrs == ("203.0.113.0/24",)

    async def test_publish_failure_without_fallback_raises(self):
        """A sandbox whose egress cannot be authorized must not be launched."""
        with pytest.raises(EgressGrantUnavailable):
            await self._store(FakeRedis(failing=True)).publish(_grant(), ttl_seconds=60)

    async def test_fetch_failure_raises_rather_than_denying_silently(self):
        with pytest.raises(EgressGrantUnavailable):
            await self._store(FakeRedis(failing=True)).fetch(TASK)

    async def test_usage_survives_a_counter_failure(self):
        """Losing the counter must not fail a transfer that already happened."""
        store = self._store(FakeRedis(failing=True))

        assert await store.add_usage(TASK, 500, ttl_seconds=60) == 500
        assert await store.usage(TASK) == 0

    async def test_revoke_clears_both_keys(self):
        client = FakeRedis()
        store = self._store(client)
        await store.publish(_grant(), ttl_seconds=60)
        await store.add_usage(TASK, 10, ttl_seconds=60)

        await store.revoke(TASK)

        assert grant_key(TASK) not in client.values
        assert usage_key(TASK) not in client.values


class TestKeys:
    def test_keys_are_namespaced_and_distinct(self):
        assert grant_key(TASK) != usage_key(TASK)
        assert grant_key(TASK).startswith("eg:grant:")
        assert usage_key(TASK).startswith("eg:used:")

    def test_ttl_margin_outlives_the_task(self):
        """A last flush landing on the deadline must not be denied."""
        assert GRANT_TTL_MARGIN_SECONDS > 0


class TestWarehouseHelper:
    def test_builds_warehouse_kind_destinations(self):
        destinations = warehouse_destinations(["WH.Example.COM ", ""], ports=[5432])

        assert len(destinations) == 1
        assert destinations[0].host == "wh.example.com"
        assert destinations[0].kind == "warehouse"
        assert destinations[0].ports == (5432,)

    def test_defaults_to_the_common_warehouse_ports(self):
        from datacode_common.egress import WAREHOUSE_PORTS

        assert warehouse_destinations(["wh.example.com"])[0].ports == WAREHOUSE_PORTS
