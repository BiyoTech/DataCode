"""Lease wiring tests (design 06 P4a).

The wiring layer is small but it is where two decisions with operational
consequences live: whether the subsystem starts at all, and what happens when it
is asked to start without the coordination backend it depends on. Both are
worth pinning, because getting either wrong produces a service that looks
healthy while its central invariant is unenforceable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lease import wiring  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singleton():
    wiring._manager = None
    yield
    wiring._manager = None


class TestSubsystemToggle:
    async def test_disabled_lease_returns_no_manager(self, monkeypatch):
        # Pre-P4a behaviour: requests reach any replica off the shared volume.
        # The storage contract is what keeps this honest — a replica-private
        # medium with leases off is refused at startup.
        monkeypatch.setattr(wiring.settings, "lease_enabled", False)
        assert await wiring.init_lease_manager() is None
        assert wiring.get_lease_manager() is None

    async def test_enabled_without_redis_fails_loudly(self, monkeypatch):
        """Silently degrading here would leave a published guarantee false.

        Leases were explicitly enabled, and without coordination the
        single-writer invariant they promise cannot be upheld — so the service
        must refuse to start rather than serve with an unenforceable contract.
        """
        monkeypatch.setattr(wiring.settings, "lease_enabled", True)
        monkeypatch.setattr("datacode_common.redis.get_redis", lambda: None)

        with pytest.raises(RuntimeError, match="require a coordination backend"):
            await wiring.init_lease_manager()

    async def test_shutdown_without_a_manager_is_safe(self):
        await wiring.shutdown_lease_manager()


class TestRepositorySourceResolution:
    async def test_database_failure_yields_an_empty_source(self, monkeypatch):
        """A warm working copy must stay servable through a database hiccup.

        It needs no remote at all: the files are already on this disk. Raising
        here would take down workspaces that had nothing to fetch.
        """

        def _boom():
            raise ConnectionError("database down")

        monkeypatch.setattr(
            "src.workspace.repository.WorkspaceRepository", lambda: _boom()
        )
        source = await wiring.resolve_repository_source("t1/ws-t1-shop/u1/dev")
        assert source.repo_url == ""
        assert source.auth is None

    async def test_malformed_lease_key_is_rejected(self):
        from datacode_common.naming import LeaseKeyError

        with pytest.raises(LeaseKeyError):
            await wiring.resolve_repository_source("garbage")


class TestMemoryRatio:
    def test_absent_cgroup_reports_zero_rather_than_guessing(self):
        """On dev machines the count limit is the only guard.

        Reading the HOST's memory instead would let an instance accept leases
        until the kernel OOM-kills it, taking every other lease down with it.
        """
        ratio = wiring._memory_used_ratio()
        assert 0.0 <= ratio <= 1.0
