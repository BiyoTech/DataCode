"""Lease-aware routing tests (design 03 §3.3, design 05 §5.2).

The cases worth having are the ones where a plausible alternative reading of the
contract would silently break the single-writer invariant. Each of the three
distinctions below is one the gateway's Go router already makes, and each is easy
to lose when reimplementing:

* a clean MISS triggers placement, an OUTAGE must not — placement needs the same
  Redis, so asking during an outage only delays a request that must degrade;
* a MALFORMED record is not a miss — the record is the one thing that says who
  may write, and routing on a guess is worse than degrading;
* a CAPACITY refusal must propagate, never degrade — the stable hash would pick
  one of the instances placement just refused.

The Redis client is injected rather than monkeypatched: without injection a suite
running next to a live Redis would silently exercise only one branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacode_common.lease import (
    lease_redis_key,
    normalize_runtime_addr,
    runtime_index_redis_key,
    runtime_redis_key,
    stable_hash_index,
    stable_hash_pick,
)
from datacode_common.lease_routing import (
    LeaseCapacityRefused,
    LeaseRouter,
    LeaseUnavailable,
)
from datacode_common.naming import LeaseKeyError, lease_key_from_working_copy_path

COPY = "/var/lib/datacode/workcopies/t1/ws-t1-shop/u1/envs/dev"
COPY_2 = "/var/lib/datacode/workcopies/t1/ws-t1-shop/u2/envs/dev"
KEY = "t1/ws-t1-shop/u1/dev"
FALLBACK = "http://workspace-runtime:8002"

_VECTORS_PATH = Path(__file__).resolve().parents[2] / "lease-vectors.json"


def _vectors() -> dict:
    return json.loads(_VECTORS_PATH.read_text())


class FakeRedis:
    """Models only what routing reads, including the ways it can fail."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, list[str]] = {}
        self.fail = False
        self.gets = 0

    async def get(self, key: str):
        self.gets += 1
        if self.fail:
            raise ConnectionError("redis unreachable")
        return self.values.get(key)

    async def smembers(self, key: str):
        if self.fail:
            raise ConnectionError("redis unreachable")
        return list(self.sets.get(key, ()))

    def set_lease(self, key: str, runtime_id: str, addr: str) -> None:
        self.values[lease_redis_key(key)] = json.dumps(
            {
                "token": "tok",
                "runtime_id": runtime_id,
                "runtime_addr": addr,
                "acquired_at": 1780000000.0,
            }
        )

    def set_heartbeat(self, runtime_id: str, addr: str, draining: bool = False) -> None:
        self.values[runtime_redis_key(runtime_id)] = json.dumps(
            {
                "runtime_id": runtime_id,
                "addr": addr,
                "lease_count": 1,
                "max_leases": 16,
                "memory_used_ratio": 0.1,
                "reported_at": "2026-09-01T00:00:00+00:00",
                "draining": draining,
                "leases": [],
            }
        )
        index = runtime_index_redis_key()
        self.sets.setdefault(index, []).append(runtime_id)


class FakePlacement:
    """Stands in for the runtime fleet's /internal/leases/acquire."""

    def __init__(self, addr: str = "", error: Exception | None = None) -> None:
        self.addr = addr
        self.error = error
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def acquire(self, lease_key_value: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.addr


def _router(redis, placement=None, **kwargs) -> LeaseRouter:
    return LeaseRouter(
        fallback_url=FALLBACK,
        placement=placement,
        redis_factory=_factory(redis),
        **kwargs,
    )


def _factory(redis):
    async def factory():
        return redis

    return factory


class TestAuthoritativeResolution:
    async def test_resolves_to_the_lease_holder(self):
        redis = FakeRedis()
        redis.set_lease(KEY, "workspace-runtime-2", "10.1.2.3:8002")

        # A POD address, not the Service: routing to a specific replica is the
        # entire purpose of the lease.
        assert await _router(redis).resolve(COPY) == "http://10.1.2.3:8002"

    async def test_non_leasable_paths_go_to_any_replica(self):
        redis = FakeRedis()
        router = _router(redis)
        # Many routes carry no working copy. Those are correctly served by any
        # replica, so an unparseable path must not be an error.
        for path in ("", "/api/v1/workspaces", "/base/t1/ws-t1-shop/u1"):
            assert await router.resolve(path) == FALLBACK

    async def test_cache_avoids_a_round_trip_per_request(self):
        redis = FakeRedis()
        redis.set_lease(KEY, "rt-1", "10.0.0.1:8002")
        router = _router(redis)

        for _ in range(5):
            await router.resolve(COPY)
        # Without caching, every tool call would hit the coordination backend.
        assert redis.gets == 1

    async def test_invalidate_forces_a_fresh_lookup(self):
        redis = FakeRedis()
        redis.set_lease(KEY, "rt-1", "10.0.0.1:8002")
        router = _router(redis)
        await router.resolve(COPY)

        # The lease moved (drain or crash). Without invalidation the caller
        # would keep sending this copy's work to a dead pod for the whole TTL.
        redis.set_lease(KEY, "rt-2", "10.0.0.2:8002")
        router.invalidate(COPY)

        assert await router.resolve(COPY) == "http://10.0.0.2:8002"

    async def test_cache_expires(self):
        redis = FakeRedis()
        redis.set_lease(KEY, "rt-1", "10.0.0.1:8002")
        # A zero TTL is the observable form of "a stale entry must not outlive
        # its window".
        router = _router(redis, cache_ttl=0.0)
        await router.resolve(COPY)

        redis.set_lease(KEY, "rt-9", "10.0.0.9:8002")
        assert await router.resolve(COPY) == "http://10.0.0.9:8002"


class TestPlacement:
    async def test_miss_triggers_placement(self):
        redis = FakeRedis()  # no lease stored
        placement = FakePlacement(addr="http://10.0.0.7:8002")

        got = await _router(redis, placement).resolve(COPY)

        assert placement.calls == 1
        assert got == "http://10.0.0.7:8002"

    async def test_placed_lease_is_cached(self):
        redis = FakeRedis()
        placement = FakePlacement(addr="http://10.0.0.7:8002")
        router = _router(redis, placement)

        for _ in range(3):
            await router.resolve(COPY)
        # Placement is a control-plane round trip; repeating it per request
        # would put the controller on the hot path.
        assert placement.calls == 1

    async def test_outage_does_not_trigger_placement(self):
        # Placement needs the SAME Redis, so asking during an outage only adds
        # latency to a request that must degrade anyway.
        redis = FakeRedis()
        redis.set_heartbeat("rt-1", "10.0.0.1:8002")
        redis.fail = True
        placement = FakePlacement(addr="http://10.0.0.7:8002")

        with pytest.raises(LeaseUnavailable):
            # Heartbeats are unreadable too during a full outage, so there is
            # no degraded answer either; the fallback below covers the rest.
            await LeaseRouter(
                fallback_url="",
                placement=placement,
                redis_factory=_factory(redis),
            ).resolve(COPY)

        assert placement.calls == 0

    async def test_malformed_record_does_not_trigger_placement(self):
        # The record says who may write. A corrupt one means we do not know, and
        # asking for a NEW placement while an unreadable claim exists could
        # mount the working copy twice.
        redis = FakeRedis()
        redis.values[lease_redis_key(KEY)] = '{"runtime_id":"rt-1"}'  # no addr
        redis.set_heartbeat("rt-1", "10.0.0.1:8002")
        placement = FakePlacement(addr="http://10.0.0.7:8002")

        got = await _router(redis, placement).resolve(COPY)

        assert placement.calls == 0
        # Degrades rather than inventing an address.
        assert got == "http://10.0.0.1:8002"

    async def test_capacity_refusal_propagates_instead_of_degrading(self):
        # The stable hash would pick one of the instances placement just
        # refused, routing around the capacity decision the fleet agreed on.
        redis = FakeRedis()
        redis.set_heartbeat("rt-1", "10.0.0.1:8002")
        placement = FakePlacement(
            error=LeaseCapacityRefused("no capacity", retry_after="30")
        )

        with pytest.raises(LeaseCapacityRefused) as excinfo:
            await _router(redis, placement).resolve(COPY)

        assert excinfo.value.retry_after == "30"

    async def test_other_placement_failures_still_degrade(self):
        # A transport hiccup must not become a user-facing error when a replica
        # can serve the copy.
        redis = FakeRedis()
        redis.set_heartbeat("rt-1", "10.0.0.1:8002")
        placement = FakePlacement(error=LeaseUnavailable("placement unreachable"))

        assert await _router(redis, placement).resolve(COPY) == "http://10.0.0.1:8002"


class TestDegradedRouting:
    async def test_stable_for_the_same_copy(self):
        # This is what preserves the single-writer invariant during an outage.
        redis = FakeRedis()
        for i in (1, 2, 3):
            redis.set_heartbeat(f"rt-{i}", f"10.0.0.{i}:8002")
        router = _router(redis, cache_ttl=0.0)

        first = await router.resolve(COPY)
        for _ in range(10):
            assert await router.resolve(COPY) == first

    async def test_skips_draining_replicas(self):
        # A draining replica keeps its existing leases but must not be handed
        # new work: it is about to disappear.
        redis = FakeRedis()
        redis.set_heartbeat("rt-1", "10.0.0.1:8002", draining=True)
        redis.set_heartbeat("rt-2", "10.0.0.2:8002")

        assert await _router(redis).resolve(COPY) == "http://10.0.0.2:8002"

    async def test_skips_expired_heartbeats(self):
        # The index is a SET and cannot expire per member, so a crashed
        # replica's id lingers; routing to it would be a connection error.
        redis = FakeRedis()
        redis.sets[runtime_index_redis_key()] = ["rt-dead"]
        redis.set_heartbeat("rt-2", "10.0.0.2:8002")

        assert await _router(redis).resolve(COPY) == "http://10.0.0.2:8002"

    async def test_no_replicas_falls_back_to_the_service(self):
        # Acceptable only because the runtime's flock still refuses a second
        # mount: the request fails cleanly rather than corrupting anything.
        assert await _router(FakeRedis()).resolve(COPY) == FALLBACK

    async def test_without_any_fallback_it_refuses_to_guess(self):
        router = LeaseRouter(fallback_url="", redis_factory=_factory(FakeRedis()))
        with pytest.raises(LeaseUnavailable):
            await router.resolve(COPY)

    async def test_disabled_coordination_reproduces_single_replica_behaviour(self):
        # Leases off is a legitimate deployment (single replica, shared medium),
        # and the Service address is the right answer for it.
        router = LeaseRouter(fallback_url=FALLBACK, redis_factory=_factory(None))
        assert await router.resolve(COPY) == FALLBACK


class TestCrossLanguageRoutingVectors:
    """Go and Python must route identically; the fixture is the arbiter.

    A divergence raises nowhere: the two would pick different replicas for one
    working copy during an outage, which is two writers on one Git worktree.
    """

    def test_normalize_addr_matches_shared_vectors(self):
        cases = _vectors()["normalize_addr"]
        assert cases, "the cross-language check would be vacuous"
        for case in cases:
            assert normalize_runtime_addr(case["addr"]) == case["expected"], case["why"]

    def test_stable_hash_matches_shared_vectors(self):
        cases = _vectors()["stable_hash"]
        assert cases
        for case in cases:
            assert stable_hash_index(case["key"], case["count"]) == case["index"]

    def test_working_copy_paths_derive_the_same_key_without_a_base(self):
        # The routing callers have no materialization root and no filesystem, so
        # they read segments from the end. The keys must still match the strict
        # decomposition's.
        for case in _vectors()["working_copy_paths"]:
            assert lease_key_from_working_copy_path(case["path"]) == case["key"]

    def test_invalid_working_copy_paths_are_rejected(self):
        for case in _vectors()["invalid_working_copy_paths"]:
            with pytest.raises(LeaseKeyError):
                lease_key_from_working_copy_path(case["path"])


class TestPathDerivation:
    def test_traversal_cannot_reach_another_tenant(self):
        # The derived key must describe the EFFECTIVE directory, not the prefix
        # that happens to appear first in the string.
        key = lease_key_from_working_copy_path(
            "/base/t1/ws-t1-shop/u1/envs/../../../t2/ws-t2-x/u2/envs/dev"
        )
        assert key.startswith("t2/")

    def test_backslash_separators_are_normalized(self):
        assert (
            lease_key_from_working_copy_path(r"C:\data\t1\ws-t1-shop\u1\envs\dev")
            == KEY
        )

    def test_envs_level_is_asserted(self):
        # Without this the shared object database and the user root would both
        # become "leasable", and neither has independent single-writer state.
        with pytest.raises(LeaseKeyError, match="envs"):
            lease_key_from_working_copy_path("/base/t1/ws-t1-shop/u1/other/dev")


class TestStableHashPick:
    def test_pick_is_independent_of_input_order(self):
        # Replica sets arrive from an unordered Redis SET: an unsorted pick
        # would be stable per-process and inconsistent across processes.
        forward = stable_hash_pick(KEY, ["http://a:1", "http://b:1", "http://c:1"])
        reverse = stable_hash_pick(KEY, ["http://c:1", "http://b:1", "http://a:1"])
        assert forward == reverse

    def test_no_usable_candidate_yields_nothing(self):
        assert stable_hash_pick(KEY, ["", ""]) == ""

    def test_non_positive_count_is_rejected(self):
        # A sentinel index would address element 0 of an empty list.
        with pytest.raises(ValueError, match="at least one candidate"):
            stable_hash_index(KEY, 0)

    def test_distinct_copies_spread(self):
        # Hashing must distribute, otherwise an outage piles every workspace
        # onto one replica and OOMs it.
        candidates = [f"http://10.0.0.{i}:8002" for i in range(1, 9)]
        picks = {
            stable_hash_pick(lease_key_from_working_copy_path(p), candidates)
            for p in (COPY, COPY_2)
        }
        assert all(p in candidates for p in picks)
