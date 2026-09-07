"""LeaseBinder tests — the reconciler's fifth party (design 05 §5.1).

This binder is the only new component that talks to Redis directly, so what
needs pinning is its READ discipline rather than any decision (the decisions live
in the reconciler and are tested there):

* SCAN, never KEYS — the pass runs against the same Redis the request path uses;
* an unreadable backend yields ``None``, never an empty world, because "no
  leases exist" would authorize deleting every code location and settling every
  task;
* keys that expire mid-pass, undecodable payloads and foreign keys are skipped
  individually instead of aborting the pass;
* the cell prefix is part of the scan pattern, so one cell never enumerates
  another's leases;
* releasing is a compare-and-swap on the token that was observed.
"""

from __future__ import annotations

import json
import time

import pytest
from datacode_common.lease import (
    lease_redis_key,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.environments.lease_binder import LeaseBinder

_KEY = "t1/ws-t1-sales/alice/dev_alice"
_KEY2 = "t1/ws-t1-sales/bob/dev_bob"


class FakeRedis:
    """Implements SCAN/GET/SMEMBERS plus the CAS delete, and records usage."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self.scan_calls = 0
        self.keys_called = False
        self.fail = False
        # Keys to drop the moment they are GET, modelling a TTL that lapsed
        # between the SCAN that listed the key and the GET that reads it.
        self.vanish_on_get: set[str] = set()

    def _boom(self) -> None:
        if self.fail:
            raise ConnectionError("redis unreachable")

    async def scan(self, cursor=0, match=None, count=10):
        self._boom()
        self.scan_calls += 1
        keys = [key for key in sorted(self._values) if _matches(match, key)]
        # One page per call, so a test can assert the loop terminates on the
        # cursor rather than on running out of keys.
        page = keys[cursor : cursor + count]
        next_cursor = cursor + count if cursor + count < len(keys) else 0
        return next_cursor, page

    async def keys(self, pattern):  # pragma: no cover - must never be called
        self.keys_called = True
        raise AssertionError("KEYS blocks the server; the binder must use SCAN")

    async def get(self, key):
        self._boom()
        if key in self.vanish_on_get:
            self._values.pop(key, None)
            return None
        return self._values.get(key)

    async def smembers(self, key):
        self._boom()
        return set(self._sets.get(key, set()))

    async def eval(self, script, _numkeys, key, *args):
        """The CAS delete, by its observable behaviour."""
        self._boom()
        stored = self._values.get(key)
        if not stored:
            return 0
        try:
            owns = json.loads(stored).get("token") == args[0]
        except ValueError:
            return 0
        if owns:
            del self._values[key]
            return 1
        return 0

    # Test helpers -------------------------------------------------------

    def put_lease(self, lease_key: str, runtime_id: str, token: str = "tok") -> str:
        full = lease_redis_key(lease_key)
        self._values[full] = json.dumps(
            {
                "token": token,
                "runtime_id": runtime_id,
                "runtime_addr": f"{runtime_id}:8002",
                "acquired_at": time.time(),
            }
        )
        return full

    def put_raw(self, full_key: str, value: str) -> None:
        self._values[full_key] = value

    def put_heartbeat(self, runtime_id: str, *leases: str) -> None:
        self._values[runtime_redis_key(runtime_id)] = json.dumps(
            {
                "runtime_id": runtime_id,
                "addr": f"{runtime_id}:8002",
                "lease_count": len(leases),
                "max_leases": 10,
                "memory_used_ratio": 0.2,
                "reported_at": "2026-08-29T00:00:00+00:00",
                "leases": list(leases),
            }
        )
        self._sets.setdefault(runtime_index_redis_key(), set()).add(runtime_id)

    def has(self, lease_key: str) -> bool:
        return lease_redis_key(lease_key) in self._values


def _matches(pattern: str | None, key: str) -> bool:
    """Glob matching limited to the single trailing ``*`` the binder uses."""
    if not pattern:
        return True
    if pattern.endswith("*"):
        return key.startswith(pattern[:-1])
    return key == pattern


@pytest.fixture()
def client() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def binder(client: FakeRedis) -> LeaseBinder:
    return LeaseBinder(client_factory=lambda: client)


class TestReadWorld:
    async def test_reads_leases_and_heartbeats_in_one_pass(self, binder, client):
        client.put_lease(_KEY, "workspace-runtime-0")
        client.put_heartbeat("workspace-runtime-0", _KEY)

        world = await binder.read_world()

        assert set(world.leases) == {_KEY}
        assert world.leases[_KEY].runtime_id == "workspace-runtime-0"
        assert world.live_runtime_ids() == frozenset({"workspace-runtime-0"})
        assert world.truncated is False

    async def test_uses_scan_not_keys(self, binder, client):
        client.put_lease(_KEY, "workspace-runtime-0")

        await binder.read_world()

        assert client.scan_calls >= 1
        assert client.keys_called is False

    async def test_unreachable_redis_yields_none_not_an_empty_world(
        self, binder, client
    ):
        """The distinction that keeps an outage from becoming a mass deletion."""
        client.put_lease(_KEY, "workspace-runtime-0")
        client.fail = True

        assert await binder.read_world() is None

    async def test_disabled_redis_yields_none(self):
        """A deployment without Redis has no lease party, not a broken one."""
        binder = LeaseBinder(client_factory=lambda: None)
        assert await binder.read_world() is None

    async def test_key_expiring_between_scan_and_get_is_skipped(
        self, binder, client
    ):
        """The TTL doing its job is not drift."""
        full = client.put_lease(_KEY, "workspace-runtime-0")
        client.put_lease(_KEY2, "workspace-runtime-1")
        client.vanish_on_get.add(full)

        world = await binder.read_world()

        assert set(world.leases) == {_KEY2}

    async def test_undecodable_payload_is_skipped_not_fatal(self, binder, client):
        client.put_raw(lease_redis_key(_KEY), "{not json")
        client.put_lease(_KEY2, "workspace-runtime-1")

        world = await binder.read_world()

        # The readable lease still made it: one bad entry must not cost the pass.
        assert set(world.leases) == {_KEY2}

    async def test_key_with_an_unparseable_suffix_is_skipped(self, binder, client):
        """A key under the lease prefix whose suffix is not a lease key.

        Written by another version or by hand. The binder reclaims only what it
        can attribute, so this is reported and skipped rather than deleted.
        """
        prefix = lease_redis_key(_KEY).rsplit(_KEY, 1)[0]
        client.put_raw(f"{prefix}garbage", "{}")
        client.put_lease(_KEY, "workspace-runtime-0")

        world = await binder.read_world()

        assert set(world.leases) == {_KEY}

    async def test_scan_pattern_is_cell_scoped(self, binder, client):
        """One cell must never enumerate another cell's leases.

        Without the cell segment in the pattern, every cell's reconciler would
        find every other cell's leases and — finding no local registry row —
        delete them all.
        """
        client.put_lease(_KEY, "workspace-runtime-0")
        # The same lease key under a DIFFERENT cell prefix, as it would appear
        # when two cells share one Redis instance.
        foreign = lease_redis_key(_KEY).replace("cell-", "cellX-", 1)
        assert foreign != lease_redis_key(_KEY)
        client.put_raw(
            foreign, json.dumps({"token": "t", "runtime_id": "r", "acquired_at": 0})
        )

        world = await binder.read_world()

        assert set(world.leases) == {_KEY}


class TestClaimants:
    async def test_record_and_heartbeat_agreeing_is_one_claimant(
        self, binder, client
    ):
        client.put_lease(_KEY, "workspace-runtime-0")
        client.put_heartbeat("workspace-runtime-0", _KEY)

        world = await binder.read_world()

        assert world.claimants_of(_KEY) == ("workspace-runtime-0",)

    async def test_two_runtimes_claiming_one_key_are_both_reported(
        self, binder, client
    ):
        """The split-brain condition, which no single view can reveal."""
        client.put_lease(_KEY, "workspace-runtime-0")
        client.put_heartbeat("workspace-runtime-0", _KEY)
        client.put_heartbeat("workspace-runtime-1", _KEY)

        world = await binder.read_world()

        assert set(world.claimants_of(_KEY)) == {
            "workspace-runtime-0",
            "workspace-runtime-1",
        }

    async def test_heartbeat_claim_counts_even_without_a_record(
        self, binder, client
    ):
        """A holder mid-degradation may hold the mount with no Redis record."""
        client.put_lease(_KEY, "workspace-runtime-0")
        client.put_heartbeat("workspace-runtime-0", _KEY)
        client.put_heartbeat("workspace-runtime-1", _KEY2)

        world = await binder.read_world()

        assert world.claimants_of(_KEY2) == ("workspace-runtime-1",)


class TestReleaseStale:
    async def test_releases_the_claim_it_observed(self, binder, client):
        client.put_lease(_KEY, "workspace-runtime-0", token="observed")
        world = await binder.read_world()

        assert await binder.release_stale(world.leases[_KEY]) is True
        assert not client.has(_KEY)

    async def test_does_not_release_a_successors_claim(self, binder, client):
        """A lease re-acquired between the read and the write must survive."""
        client.put_lease(_KEY, "workspace-runtime-0", token="observed")
        world = await binder.read_world()
        record = world.leases[_KEY]
        # Someone else took the lease in the meantime.
        client.put_lease(_KEY, "workspace-runtime-1", token="successor")

        assert await binder.release_stale(record) is False
        assert client.has(_KEY)

    async def test_release_failure_is_reported_not_raised(self, binder, client):
        """The TTL is the backstop; a failed repair is next pass's work."""
        client.put_lease(_KEY, "workspace-runtime-0")
        world = await binder.read_world()
        client.fail = True

        assert await binder.release_stale(world.leases[_KEY]) is False
