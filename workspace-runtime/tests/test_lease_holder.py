"""Lease holder and heartbeat tests (design 03 §3.3, P4a).

The fake Redis below implements the three semantics the holder actually relies
on — ``SET NX``, per-key expiry, and the compare-and-swap Lua scripts. A mock
that merely records calls would only assert that this code calls the functions
it calls; what needs pinning is the *outcome* when two instances race, when a
lease is re-granted behind a holder's back, and when Redis is unreachable.

The cases here are the ones where a plausible implementation silently produces
two writers on one worktree.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.lease import (  # noqa: E402
    LeaseState,
    lease_redis_key,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.lease.heartbeat import HeartbeatReporter  # noqa: E402
from src.lease.holder import (  # noqa: E402
    LeaseHolder,
    LeaseSplitBrain,
    LeaseUnavailable,
)

_KEY = "t1/ws-t1-shop/u1/dev"
_KEY2 = "t1/ws-t1-shop/u2/dev"


class FakeRedis:
    """Minimal Redis honouring NX, expiry and the CAS scripts."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}
        self.fail = False

    def _boom(self) -> None:
        if self.fail:
            raise ConnectionError("redis unreachable")

    def _sweep(self) -> None:
        now = time.monotonic()
        for key, deadline in list(self._expiry.items()):
            if deadline <= now:
                self._values.pop(key, None)
                self._expiry.pop(key, None)

    async def set(self, key, value, nx=False, px=None, ex=None):
        self._boom()
        self._sweep()
        if nx and key in self._values:
            return None
        self._values[key] = value
        if px is not None:
            self._expiry[key] = time.monotonic() + px / 1000
        elif ex is not None:
            self._expiry[key] = time.monotonic() + ex
        return True

    async def get(self, key):
        self._boom()
        self._sweep()
        return self._values.get(key)

    async def delete(self, key):
        self._boom()
        return 1 if self._values.pop(key, None) is not None else 0

    async def sadd(self, key, member):
        self._boom()
        self._sets.setdefault(key, set()).add(member)
        return 1

    async def srem(self, key, member):
        self._boom()
        self._sets.get(key, set()).discard(member)
        return 1

    async def smembers(self, key):
        self._boom()
        return set(self._sets.get(key, set()))

    async def eval(self, script, _numkeys, key, *args):
        """Execute the RELEASE/RENEW scripts by their observable behaviour."""
        self._boom()
        self._sweep()
        stored = self._values.get(key)
        token = args[0]
        owns = bool(stored) and json.loads(stored).get("token") == token
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

    # Test helpers -------------------------------------------------------
    def force_expire(self, key: str) -> None:
        self._values.pop(key, None)
        self._expiry.pop(key, None)

    def token_of(self, key: str) -> str:
        return json.loads(self._values[key])["token"]


@pytest.fixture()
def client() -> FakeRedis:
    return FakeRedis()


def _holder(client, tmp_path, **overrides) -> LeaseHolder:
    kwargs = {
        "client": client,
        "materialize_root": str(tmp_path),
        "runtime_addr": "10.1.2.3:8002",
        "ttl_seconds": 120,
    }
    kwargs.update(overrides)
    return LeaseHolder(**kwargs)


class TestAcquire:
    async def test_acquire_records_lease_and_takes_local_guard(self, client, tmp_path):
        holder = _holder(client, tmp_path)
        lease = await holder.acquire(_KEY)

        assert lease.state is LeaseState.HELD
        assert lease.runtime_id == holder.runtime_id
        assert holder.holds(_KEY)
        # Both claims must exist: Redis for routing, flock for the filesystem.
        assert await client.get(lease_redis_key(_KEY)) is not None
        assert (tmp_path / "t1" / "ws-t1-shop" / "u1" / ".datacode-lease").exists()
        await holder.release(_KEY)

    async def test_acquire_is_idempotent(self, client, tmp_path):
        # Every request for a workspace arrives here; re-acquiring must not
        # take a second flock (which would deadlock against ourselves).
        holder = _holder(client, tmp_path)
        first = await holder.acquire(_KEY)
        second = await holder.acquire(_KEY)
        assert first is second
        assert holder.held_keys() == (_KEY,)
        await holder.release(_KEY)

    async def test_second_instance_is_refused(self, client, tmp_path):
        a = _holder(client, tmp_path)
        b = _holder(client, tmp_path)
        await a.acquire(_KEY)
        try:
            with pytest.raises(LeaseUnavailable, match="leased to"):
                await b.acquire(_KEY)
        finally:
            await a.release(_KEY)

    async def test_redis_outage_refuses_rather_than_fabricating_a_lease(
        self, client, tmp_path
    ):
        # The dangerous alternative: assume ownership when coordination is
        # unavailable. That makes the instance *believe* it is the sole writer.
        client.fail = True
        holder = _holder(client, tmp_path)
        with pytest.raises(LeaseUnavailable, match="unreachable"):
            await holder.acquire(_KEY)
        assert not holder.holds(_KEY)

    async def test_local_claim_failure_gives_back_the_distributed_claim(
        self, client, tmp_path
    ):
        # Otherwise this instance blocks a workspace it cannot serve for a full
        # TTL, and the legitimate holder cannot take it.
        from src.lease.mount_guard import acquire_mount

        blocker = acquire_mount(str(tmp_path), _KEY)
        holder = _holder(client, tmp_path)
        try:
            # Lock contention is a SPLIT BRAIN (design 08 §7): Redis granted
            # the claim, the file lock says someone else owns the files. The
            # contract answer is 409 with a retry, not a capacity back-off —
            # hence the distinct exception type.
            with pytest.raises(LeaseSplitBrain, match="arbitration failed"):
                await holder.acquire(_KEY)
            assert await client.get(lease_redis_key(_KEY)) is None
        finally:
            blocker.release()

    async def test_malformed_lease_key_is_rejected(self, client, tmp_path):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            await _holder(client, tmp_path).acquire("garbage")


class TestRelease:
    async def test_release_clears_both_claims(self, client, tmp_path):
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        await holder.release(_KEY)

        assert not holder.holds(_KEY)
        assert await client.get(lease_redis_key(_KEY)) is None
        # The flock must be free for the next holder.
        await _holder(client, tmp_path).acquire(_KEY)

    async def test_release_is_idempotent(self, client, tmp_path):
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        await holder.release(_KEY)
        await holder.release(_KEY)

    async def test_release_never_deletes_a_successors_claim(self, client, tmp_path):
        """The core CAS guarantee.

        A stale holder (its lease lapsed during a pause, and the controller
        re-granted it) must not delete the new owner's claim when it finally
        releases.
        """
        stale = _holder(client, tmp_path)
        await stale.acquire(_KEY)
        full_key = lease_redis_key(_KEY)

        # Simulate the lapse + re-grant by another instance.
        client.force_expire(full_key)
        await client.set(
            full_key,
            json.dumps({"token": "successor-token", "runtime_id": "rt-9"}),
            nx=True,
            px=120_000,
        )

        await stale.release(_KEY)

        remaining = await client.get(full_key)
        assert remaining is not None, "stale holder deleted the successor's lease"
        assert json.loads(remaining)["token"] == "successor-token"

    async def test_release_all_drains_every_lease(self, client, tmp_path):
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        await holder.acquire(_KEY2)
        await holder.release_all()
        assert holder.held_keys() == ()

    async def test_release_survives_a_redis_outage(self, client, tmp_path):
        """A failed CAS delete must still free the LOCAL claim.

        The Redis record is left to expire by TTL (that is the documented
        backstop), so the workspace stays unreachable for up to one TTL. But the
        flock must be released regardless: keeping it would make this node
        unable to serve the workspace even after the record lapsed, which no
        timeout would ever repair.
        """
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        client.fail = True
        await holder.release(_KEY)
        client.fail = False

        assert not holder.holds(_KEY)
        # The stale Redis record is expected; the TTL clears it.
        assert await client.get(lease_redis_key(_KEY)) is not None
        # The local claim, however, must be gone right now.
        from src.lease.mount_guard import acquire_mount

        acquire_mount(str(tmp_path), _KEY).release()


class TestWatchdog:
    async def test_renewal_extends_the_lease(self, client, tmp_path):
        # ttl=3 -> renew every 1s; one renewal must land within the window.
        holder = _holder(client, tmp_path, ttl_seconds=3)
        await holder.acquire(_KEY)
        try:
            await asyncio.sleep(1.3)
            assert await client.get(lease_redis_key(_KEY)) is not None
            assert holder.holds(_KEY)
        finally:
            await holder.release(_KEY)

    async def test_holder_evicts_itself_when_the_lease_was_re_granted(
        self, client, tmp_path
    ):
        """A renewal reporting "not owner" means someone else owns the files.

        Continuing to serve would be the two-writer scenario, so the holder must
        drop the working copy AND free the flock.
        """
        holder = _holder(client, tmp_path, ttl_seconds=3)
        await holder.acquire(_KEY)
        full_key = lease_redis_key(_KEY)

        client.force_expire(full_key)
        await client.set(
            full_key,
            json.dumps({"token": "someone-else", "runtime_id": "rt-9"}),
            nx=True,
            px=120_000,
        )

        # Wait past one renewal interval plus the eviction task.
        for _ in range(40):
            await asyncio.sleep(0.1)
            if not holder.holds(_KEY):
                break

        assert not holder.holds(_KEY), "holder kept a lease it no longer owns"
        # The flock must be released too, or the real owner cannot mount here.
        from src.lease.mount_guard import acquire_mount

        acquire_mount(str(tmp_path), _KEY).release()

    async def test_transient_renewal_failure_does_not_drop_the_lease(
        self, client, tmp_path
    ):
        # A network blip must not cost the lease: that is why renewal runs at
        # ttl/3 rather than once per TTL.
        holder = _holder(client, tmp_path, ttl_seconds=3)
        await holder.acquire(_KEY)
        try:
            client.fail = True
            await asyncio.sleep(1.2)
            client.fail = False
            assert holder.holds(_KEY)
        finally:
            await holder.release(_KEY)


class TestDraining:
    async def test_draining_lease_stays_routable(self, client, tmp_path):
        # Requests for in-flight operations must keep arriving at the draining
        # holder; only new placements stop.
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        holder.mark_draining(_KEY)
        lease = await holder.acquire(_KEY)
        assert lease.state is LeaseState.DRAINING
        assert lease.is_routable is True
        await holder.release(_KEY)

    async def test_mark_draining_on_unknown_lease_is_a_noop(self, client, tmp_path):
        _holder(client, tmp_path).mark_draining(_KEY)


class TestHeartbeat:
    def _reporter(self, client, **overrides) -> HeartbeatReporter:
        kwargs = {
            "client": client,
            "runtime_id": "rt-0",
            "addr": "10.1.2.3:8002",
            "max_leases": 16,
            "load_provider": lambda: (2, 0.35, False, ("a/b/c/dev",)),
            "interval_seconds": 0.1,
        }
        kwargs.update(overrides)
        return HeartbeatReporter(**kwargs)

    async def test_publish_writes_heartbeat_and_joins_the_index(self, client):
        reporter = self._reporter(client)
        assert await reporter.publish_once() is True

        raw = await client.get(runtime_redis_key("rt-0"))
        payload = json.loads(raw)
        assert payload["lease_count"] == 2
        assert payload["max_leases"] == 16
        # UTC ISO-8601, so the value survives crossing a language boundary.
        assert payload["reported_at"].endswith("+00:00")
        assert "rt-0" in await client.smembers(runtime_index_redis_key())

    async def test_heartbeat_expires_so_a_dead_instance_stops_being_placeable(
        self, client
    ):
        reporter = self._reporter(client)
        await reporter.publish_once()
        key = runtime_redis_key("rt-0")
        assert await client.get(key) is not None
        # TTL is floored at 1s (a 0 TTL would mean "already expired"), so a
        # dead instance leaves placement without any reaper running.
        await asyncio.sleep(1.2)
        assert await client.get(key) is None

    async def test_subsecond_interval_still_yields_a_live_ttl(self, client):
        # Regression: int(0.1 * 3) == 0, and ex=0 makes Redis drop the key
        # instantly -- the instance would publish heartbeats that never exist
        # and silently never receive a lease.
        reporter = self._reporter(client, interval_seconds=0.1)
        await reporter.publish_once()
        assert await client.get(runtime_redis_key("rt-0")) is not None

    async def test_publish_failure_is_degradation_not_an_exception(self, client):
        # An instance that cannot publish keeps serving the leases it holds.
        client.fail = True
        assert await self._reporter(client).publish_once() is False

    async def test_start_publishes_immediately(self, client):
        reporter = self._reporter(client)
        await reporter.start()
        try:
            # Placeable at once rather than one interval after startup.
            assert await client.get(runtime_redis_key("rt-0")) is not None
        finally:
            await reporter.stop()

    async def test_stop_removes_the_instance_from_placement(self, client):
        reporter = self._reporter(client)
        await reporter.start()
        await reporter.stop()
        assert await client.get(runtime_redis_key("rt-0")) is None
        assert "rt-0" not in await client.smembers(runtime_index_redis_key())

    async def test_current_reflects_the_load_provider(self, client):
        reporter = self._reporter(
            client, load_provider=lambda: (16, 0.9, True, ())
        )
        heartbeat = reporter.current()
        assert heartbeat.draining is True
        # Draining or full: either alone must stop new placements.
        assert heartbeat.has_capacity() is False

    async def test_heartbeat_reports_capacity_when_idle(self, client):
        assert self._reporter(client).current().has_capacity() is True

    async def test_holder_can_feed_the_reporter(self, client, tmp_path):
        # The wiring the service will use: the holder owns the lease count.
        holder = _holder(client, tmp_path)
        await holder.acquire(_KEY)
        reporter = self._reporter(
            client,
            runtime_id=holder.runtime_id,
            load_provider=lambda: (
                len(holder.held_keys()),
                0.2,
                False,
                holder.held_keys(),
            ),
        )
        try:
            assert reporter.current().lease_count == 1
            assert reporter.current().leases == (_KEY,)
        finally:
            await holder.release(_KEY)
