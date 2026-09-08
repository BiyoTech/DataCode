"""LeaseManager tests (design 06 P4a).

The manager exists to make a specific mistake impossible: touching a working
copy without owning it. So the assertions here are mostly about ORDER and about
what is left behind when a step fails — properties that the four underlying
modules cannot enforce individually.

Real git and a real flock; only Redis is faked, and faithfully enough that the
compare-and-swap semantics the holder relies on still hold.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.lease import lease_redis_key  # noqa: E402

from src.lease.holder import LeaseHolder, LeaseUnavailable  # noqa: E402
from src.lease.manager import LeaseManager, RepositorySource  # noqa: E402
from src.lease.materialize import MaterializationError  # noqa: E402
from src.lease.wip import take_snapshot, wip_ref_for_lease  # noqa: E402

_KEY = "t1/ws-t1-shop/u1/dev"
_KEY2 = "t1/ws-t1-shop/u2/dev"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for lease manager tests"
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


class FakeRedis:
    """Redis with real NX / expiry / compare-and-swap behaviour."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}
        self.fail = False

    def _boom(self):
        if self.fail:
            raise ConnectionError("redis unreachable")

    def _sweep(self):
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

    async def eval(self, script, _numkeys, key, *args):
        self._boom()
        self._sweep()
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
def client() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def root(tmp_path: Path) -> str:
    path = tmp_path / "workcopies"
    path.mkdir()
    return str(path)


def _manager(client, root, remote, ttl_seconds: int = 120, **overrides) -> LeaseManager:
    calls: list[str] = []

    async def source_provider(lease_key: str) -> RepositorySource:
        calls.append(lease_key)
        return RepositorySource(repo_url=remote)

    holder = LeaseHolder(
        client=client,
        materialize_root=root,
        runtime_addr="10.1.2.3:8002",
        ttl_seconds=ttl_seconds,
    )
    kwargs = {
        "holder": holder,
        "materialize_root": root,
        "source_provider": source_provider,
        "wip_interval_seconds": 3600,  # long: tests drive snapshots explicitly
    }
    kwargs.update(overrides)
    manager = LeaseManager(**kwargs)
    manager._test_source_calls = calls  # type: ignore[attr-defined]
    return manager


def _configure_identity(workspace_path: str) -> None:
    _git(workspace_path, "config", "user.email", "u1@example.com")
    _git(workspace_path, "config", "user.name", "U1")


class TestServe:
    async def test_serve_acquires_lease_and_materializes(self, client, root, remote):
        manager = _manager(client, root, remote)
        served = await manager.serve(_KEY)

        assert served.freshly_materialized is True
        assert Path(served.workspace_path, "model.sql").exists()
        # Ownership must exist: the path was only reachable through the lease.
        assert await client.get(lease_redis_key(_KEY)) is not None
        assert manager.served_keys() == (_KEY,)

    async def test_warm_path_is_idempotent_and_does_not_refetch_source(
        self, client, root, remote
    ):
        # Every request for a leased workspace goes through serve(); a second
        # database lookup per request would be pure overhead.
        manager = _manager(client, root, remote)
        first = await manager.serve(_KEY)
        second = await manager.serve(_KEY)

        assert first is second
        assert second.freshly_materialized is True  # cached object, not re-run
        assert manager._test_source_calls == [_KEY]

    async def test_concurrent_first_touch_materializes_once(
        self, client, root, remote
    ):
        """Two requests arriving together must not race on the flock.

        Without per-lease serialization the second would try to materialize
        while the first held the local guard, and fail against its OWN instance.
        """
        import asyncio

        manager = _manager(client, root, remote)
        results = await asyncio.gather(
            manager.serve(_KEY), manager.serve(_KEY), manager.serve(_KEY)
        )
        assert len({id(r) for r in results}) == 1
        assert manager._test_source_calls == [_KEY]

    async def test_second_instance_cannot_serve_the_same_copy(
        self, client, root, remote
    ):
        a = _manager(client, root, remote)
        b = _manager(client, root, remote)
        await a.serve(_KEY)

        with pytest.raises(LeaseUnavailable):
            await b.serve(_KEY)
        assert b.served_keys() == ()

    async def test_redis_outage_refuses_before_touching_disk(
        self, client, root, remote
    ):
        """Ownership is checked FIRST, so a coordination outage writes nothing."""
        client.fail = True
        manager = _manager(client, root, remote)

        with pytest.raises(LeaseUnavailable):
            await manager.serve(_KEY)

        # No materialization was attempted: the tenant directory must not exist.
        assert not Path(root, "t1").exists()

    async def test_malformed_lease_key_is_rejected(self, client, root, remote):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            await _manager(client, root, remote).serve("garbage")


class TestFailureLeavesNoPartialState:
    async def test_lease_is_released_when_materialization_fails(
        self, client, root, tmp_path
    ):
        """A lease held for an unusable copy blocks the workspace for a TTL.

        It also denies it to the instance that could have served it, so the
        lease must be given back rather than waiting for expiry.
        """
        manager = _manager(client, root, str(tmp_path / "missing.git"))

        with pytest.raises(MaterializationError):
            await manager.serve(_KEY)

        assert await client.get(lease_redis_key(_KEY)) is None
        assert manager.served_keys() == ()

    async def test_local_guard_is_freed_after_a_failed_serve(
        self, client, root, tmp_path, remote
    ):
        # Otherwise this node could never serve the workspace again, and no
        # timeout would repair it.
        broken = _manager(client, root, str(tmp_path / "missing.git"))
        with pytest.raises(MaterializationError):
            await broken.serve(_KEY)

        working = _manager(client, root, remote)
        served = await working.serve(_KEY)
        assert Path(served.workspace_path, "model.sql").exists()


class TestOwnershipLostUnderneath:
    """What happens when a lease ends without anyone calling release().

    The Redis record can vanish while this instance keeps running: the backend
    restarts, the key is evicted, or the container is suspended past the TTL.
    The watchdog then evicts locally — and everything derived from that
    ownership has to go with it. Otherwise the instance still advertises the
    copy to placement, still answers "already yours" to an adoption, and still
    answers 409 LEASE_NOT_HELD on the request path: a permanent loop for that
    working copy that only a restart breaks.
    """

    async def _wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return predicate()

    async def test_lost_lease_stops_being_served_and_advertised(
        self, client, root, remote
    ):
        # ttl=3 -> the watchdog renews every second.
        manager = _manager(client, root, remote, ttl_seconds=3)
        await manager.serve(_KEY)

        # Ownership disappears underneath the holder.
        client._values.pop(lease_redis_key(_KEY))

        assert await self._wait_for(lambda: not manager.holds(_KEY))
        # served_keys() feeds the heartbeat: an instance that keeps counting a
        # copy it lost keeps winning placement for it.
        assert await self._wait_for(lambda: manager.served_keys() == ())

    async def test_lost_lease_can_be_adopted_again(self, client, root, remote):
        """The regression: adoption must really re-take the lease.

        A stale served entry made adopt() return success without acquiring,
        so the gateway routed here on the strength of that answer and the
        ownership gate rejected every request that followed.
        """
        manager = _manager(client, root, remote, ttl_seconds=3)
        await manager.serve(_KEY)
        client._values.pop(lease_redis_key(_KEY))
        assert await self._wait_for(lambda: not manager.holds(_KEY))

        await manager.adopt(_KEY)

        assert manager.holds(_KEY)
        assert await client.get(lease_redis_key(_KEY)) is not None
        assert await self._wait_for(lambda: manager.served_keys() == (_KEY,))

    async def test_serve_reacquires_when_bookkeeping_outlived_the_lease(
        self, client, root, remote
    ):
        """Defence in depth: the served set is never trusted on its own.

        The divergence is injected directly here rather than through the
        watchdog, because the guarantee must hold for ANY future path that
        drops ownership without telling the manager.
        """
        manager = _manager(client, root, remote)
        await manager.serve(_KEY)

        holder = manager._holder
        held = holder._held.pop(_KEY)
        held.watchdog.cancel()
        held.guard.release()
        client._values.pop(lease_redis_key(_KEY))

        served = await manager.serve(_KEY)

        assert manager.holds(_KEY)
        assert Path(served.workspace_path, "model.sql").exists()

    async def test_adopting_an_already_served_copy_frees_the_lock(
        self, client, root, remote
    ):
        """A racing adoption must not strand the per-lease lock.

        The loser takes the "already served" branch after waiting for the
        winner's materialization; leaking the lock there would make every
        later request for this copy wait forever.
        """
        manager = _manager(client, root, remote)
        # Bounded: a leaked lock makes the loser wait forever, and a hanging
        # test reports nothing useful.
        await asyncio.wait_for(
            asyncio.gather(manager.adopt(_KEY), manager.adopt(_KEY)), timeout=30
        )

        assert await self._wait_for(lambda: manager.served_keys() == (_KEY,))
        assert not manager._locks[_KEY].locked()
        # The observable consequence, not just the internal state.
        await asyncio.wait_for(manager.serve(_KEY), timeout=5)


class TestRelease:
    async def test_release_snapshots_then_gives_up_ownership(
        self, client, root, remote
    ):
        manager = _manager(client, root, remote)
        served = await manager.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 99;\n")

        await manager.release(_KEY)

        # Uncommitted work reached the server BEFORE ownership was surrendered.
        assert _git(
            remote, "rev-parse", "--verify", "--quiet", wip_ref_for_lease(_KEY),
            check=False,
        ).returncode == 0
        assert await client.get(lease_redis_key(_KEY)) is None
        assert manager.served_keys() == ()

    async def test_release_of_an_unserved_lease_is_safe(self, client, root, remote):
        await _manager(client, root, remote).release(_KEY)

    async def test_released_copy_can_be_served_by_another_instance(
        self, client, root, remote
    ):
        a = _manager(client, root, remote)
        served = await a.serve(_KEY)
        _configure_identity(served.workspace_path)
        await a.release(_KEY)

        b = _manager(client, root, remote)
        assert (await b.serve(_KEY)).workspace_path == served.workspace_path


class TestDrain:
    async def test_drain_snapshots_everything_then_releases(
        self, client, root, remote
    ):
        """The zero-loss shutdown path."""
        manager = _manager(client, root, remote)
        for key in (_KEY, _KEY2):
            served = await manager.serve(key)
            _configure_identity(served.workspace_path)
            Path(served.workspace_path, "model.sql").write_text(f"-- {key}\n")

        await manager.drain()

        for key in (_KEY, _KEY2):
            assert _git(
                remote, "rev-parse", "--verify", "--quiet", wip_ref_for_lease(key),
                check=False,
            ).returncode == 0
            assert await client.get(lease_redis_key(key)) is None
        assert manager.served_keys() == ()

    async def test_drain_stops_attracting_new_leases_first(
        self, client, root, remote
    ):
        # A heartbeat that outlived the drain would keep the controller placing
        # work on an instance that is going away.
        from src.lease.heartbeat import HeartbeatReporter

        reporter = HeartbeatReporter(
            client=client,
            runtime_id="rt-0",
            addr="10.1.2.3:8002",
            max_leases=16,
            load_provider=lambda: (0, 0.1, False, ()),
            interval_seconds=3600,
        )
        manager = _manager(client, root, remote, heartbeat=reporter)
        await manager.start()
        await manager.serve(_KEY)

        await manager.drain()

        from datacode_common.lease import runtime_redis_key

        assert await client.get(runtime_redis_key("rt-0")) is None


class TestWipRecoveryHandover:
    async def test_snapshot_from_a_previous_holder_is_surfaced(
        self, client, root, remote, tmp_path
    ):
        """The handover the durability contract promises.

        Instance A crashes with unsaved work; instance B must OFFER it rather
        than apply it, because the user may have abandoned those edits.
        """
        a = _manager(client, root, remote)
        served = await a.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 777;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=served.workspace_path,
            repo_path=served.repo_path,
        )

        # A disappears with its disk.
        await a.drain()
        shutil.rmtree(root)
        Path(root).mkdir()

        b = _manager(client, root, remote)
        recovered = await b.serve(_KEY)

        assert recovered.needs_recovery_prompt is True
        # NOT applied automatically.
        assert Path(recovered.workspace_path, "model.sql").read_text() == "SELECT 1;\n"

    async def test_user_can_recover_the_snapshot(self, client, root, remote):
        a = _manager(client, root, remote)
        served = await a.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 777;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=served.workspace_path,
            repo_path=served.repo_path,
        )
        await a.drain()
        shutil.rmtree(root)
        Path(root).mkdir()

        b = _manager(client, root, remote)
        recovered = await b.serve(_KEY)
        _configure_identity(recovered.workspace_path)

        assert await b.recover_snapshot(_KEY) is True
        assert Path(recovered.workspace_path, "model.sql").read_text() == "SELECT 777;\n"
        # The prompt must not reappear once the decision was made.
        assert b.served_copy(_KEY).needs_recovery_prompt is False

    async def test_user_can_discard_the_snapshot(self, client, root, remote):
        a = _manager(client, root, remote)
        served = await a.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 777;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=served.workspace_path,
            repo_path=served.repo_path,
        )
        await a.drain()
        shutil.rmtree(root)
        Path(root).mkdir()

        b = _manager(client, root, remote)
        await b.serve(_KEY)

        assert await b.discard_snapshot(_KEY) is True
        # Gone from the server, so it cannot be offered again.
        assert _git(
            remote, "rev-parse", "--verify", "--quiet", wip_ref_for_lease(_KEY),
            check=False,
        ).returncode != 0
        assert b.served_copy(_KEY).needs_recovery_prompt is False

    async def test_pending_copy_is_excluded_from_periodic_snapshots(
        self, client, root, remote
    ):
        """Snapshotting it would destroy the very thing awaiting a decision."""
        a = _manager(client, root, remote)
        served = await a.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 777;\n")
        take_snapshot(
            lease_key=_KEY,
            workspace_path=served.workspace_path,
            repo_path=served.repo_path,
        )
        await a.drain()
        # Read the tip AFTER drain: drain takes a final snapshot of its own, so
        # capturing it earlier would compare against a superseded commit.
        original = _git(remote, "rev-parse", wip_ref_for_lease(_KEY)).stdout.strip()
        shutil.rmtree(root)
        Path(root).mkdir()

        b = _manager(client, root, remote)
        pending = await b.serve(_KEY)
        assert pending.needs_recovery_prompt is True
        _configure_identity(pending.workspace_path)
        # Dirty the copy, then run the loop the scheduler would run.
        Path(pending.workspace_path, "other.sql").write_text("SELECT 0;\n")
        await b._snapshots.snapshot_all()

        assert _git(
            remote, "rev-parse", wip_ref_for_lease(_KEY)
        ).stdout.strip() == original

    async def test_recovery_on_an_unserved_lease_is_refused(
        self, client, root, remote
    ):
        with pytest.raises(LeaseUnavailable):
            await _manager(client, root, remote).recover_snapshot(_KEY)


class TestObservability:
    async def test_protection_status_reports_served_and_unprotected(
        self, client, root, remote
    ):
        manager = _manager(client, root, remote)
        served = await manager.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 5;\n")

        await manager._snapshots.snapshot_all()
        status = manager.protection_status()

        assert status["served"] == 1
        assert status["unprotected"] == []
        # Feeds the frontend's "last protected at" indicator.
        assert _KEY in status["last_protected_at"]

    async def test_unreachable_server_is_reported_as_unprotected(
        self, client, root, remote
    ):
        # A user who believes they are protected and is not is the worst state.
        manager = _manager(client, root, remote)
        served = await manager.serve(_KEY)
        _configure_identity(served.workspace_path)
        Path(served.workspace_path, "model.sql").write_text("SELECT 6;\n")
        shutil.rmtree(remote)

        await manager._snapshots.snapshot_all()

        assert manager.protection_status()["unprotected"] == [_KEY]
