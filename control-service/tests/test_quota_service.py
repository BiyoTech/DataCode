"""Quota enforcement tests (design 06 P8).

Pins the contract the acceptance gate names explicitly:

* a tenant at its ceiling gets 429 + ``Retry-After`` and a structured payload
  (resource / limit / current / tenant), while other tenants are unaffected;
* a missing ``tenant_quotas`` row means the platform DEFAULTS, and a zero in a
  row is an explicit ban, not "fall back to the default";
* archived/failed environments stop consuming the tenant's environment budget
  (a destroyed-and-recreated sandbox must not count twice);
* the check fails OPEN when the counters cannot be read — quota is capacity
  governance, not a security boundary, and an unreadable usage table must not
  block every tenant's creations.

Runs against a throwaway SQLite database through the service's own session
machinery: the queries are the code under test, so substituting them with fakes
would prove nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.workspace.database as database
from src.config import settings
from src.environments import models as env_models
from src.quota import get_quota_service
from src.quota.models import TenantQuotaModel, TenantUsageModel
from src.utils.errors import QuotaExceededError
from src.workspace.models import WorkspaceModel

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
async def quota_db(tmp_path, monkeypatch):
    """A throwaway SQLite database wired into the service's session factory."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'quota.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    await database.init_db()
    yield
    await database.close_db()


async def _add_workspace(tenant_id: str, name: str, status: str = "active") -> None:
    async with database.get_session() as session:
        session.add(
            WorkspaceModel(
                id=f"ws-{tenant_id}-{name}",
                tenant_id=tenant_id,
                owner_user_id="alice",
                name=name,
                status=status,
            )
        )


async def _add_environment(
    tenant_id: str, name: str, status: str, workspace_id: str = "ws-t1-sales"
) -> None:
    async with database.get_session() as session:
        session.add(
            env_models.EnvironmentModel(
                id=f"{workspace_id}:{name}",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                name=name,
                branch=f"env/{name}",
                owner_user_id="alice",
                status=status,
            )
        )


class TestWorkspaceQuota:
    async def test_at_ceiling_rejects_with_429_and_retry_after(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 2)
        for name in ("a", "b"):
            await _add_workspace("t1", name)
        with pytest.raises(QuotaExceededError) as exc_info:
            await get_quota_service().check_workspace_creation("t1")

        assert exc_info.value.status_code == 429
        assert exc_info.value.headers == {"Retry-After": "60"}
        payload = exc_info.value.detail
        assert payload["quota_resource"] == "workspace"
        assert payload["quota_limit"] == 2
        assert payload["quota_current"] == 2

    async def test_other_tenants_are_unaffected(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 1)
        await _add_workspace("t1", "a")
        # t1 is at its ceiling, t2 starts clean: quota is per tenant.
        await get_quota_service().check_workspace_creation("t2")

    async def test_deleted_workspaces_do_not_consume_the_budget(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 1)
        await _add_workspace("t1", "a", status="deleted")
        await get_quota_service().check_workspace_creation("t1")

    async def test_row_override_beats_the_default(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 50)
        async with database.get_session() as session:
            session.add(
                TenantQuotaModel(tenant_id="t1", max_workspaces=1)
            )
        await _add_workspace("t1", "a")
        with pytest.raises(QuotaExceededError) as exc_info:
            await get_quota_service().check_workspace_creation("t1")
        assert exc_info.value.detail["quota_limit"] == 1

    async def test_zero_in_a_row_is_an_explicit_ban(self, quota_db, monkeypatch):
        async with database.get_session() as session:
            session.add(TenantQuotaModel(tenant_id="t1", max_workspaces=0))
        with pytest.raises(QuotaExceededError) as exc_info:
            await get_quota_service().check_workspace_creation("t1")
        assert exc_info.value.detail["quota_limit"] == 0

    async def test_usage_snapshot_is_refreshed_on_check(self, quota_db):
        await _add_workspace("t1", "a")
        await get_quota_service().check_workspace_creation("t1")
        async with database.get_session() as session:
            row = await session.get(TenantUsageModel, "t1")
        assert row is not None
        assert row.live_workspaces == 1


class TestEnvironmentQuota:
    async def test_live_environments_count_toward_the_ceiling(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_environments", 2)
        await _add_environment("t1", "dev", "active")
        await _add_environment("t1", "stg", "creating")
        with pytest.raises(QuotaExceededError) as exc_info:
            await get_quota_service().check_environment_creation("t1")
        assert exc_info.value.detail["quota_resource"] == "environment"
        assert exc_info.value.detail["quota_current"] == 2

    async def test_archived_and_failed_environments_are_free(self, quota_db, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_environments", 2)
        await _add_environment("t1", "dev", "archived")
        await _add_environment("t1", "stg", "failed")
        await _add_environment("t1", "prod", "active")
        await get_quota_service().check_environment_creation("t1")


class TestFailOpen:
    async def test_unreadable_counters_skip_the_check(self, quota_db, monkeypatch):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _broken_session():
            # The fail-open contract lives around the DB access itself, so the
            # break must be injected BELOW the counting method.
            raise RuntimeError("database is unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr("src.quota.service.get_session", _broken_session)
        # No raise: a capacity check must not become a hard outage.
        await get_quota_service().check_workspace_creation("t1")


class TestLeaseUsageReporting:
    async def test_concurrent_and_peak_are_recorded(self, quota_db):
        service = get_quota_service()
        await service.apply_lease_usage({"t1": 5})
        await service.apply_lease_usage({"t1": 3})
        async with database.get_session() as session:
            row = await session.get(TenantUsageModel, "t1")
        assert row.concurrent_leases == 3
        # A transient dip must not erase the high-water mark.
        assert row.peak_concurrent_leases == 5

    async def test_peak_only_grows(self, quota_db):
        service = get_quota_service()
        await service.apply_lease_usage({"t1": 5})
        await service.apply_lease_usage({"t1": 9})
        await service.apply_lease_usage({"t1": 2})
        async with database.get_session() as session:
            row = await session.get(TenantUsageModel, "t1")
        assert row.peak_concurrent_leases == 9


class TestQuotaOverview:
    """The read half of enforcement (``GET /api/v1/quotas``).

    The property that matters most is agreement with the enforcement path: a
    user who sees "12/50" and is then rejected has been actively misled, so the
    overview must count the same rows ``check_*`` counts.
    """

    async def test_reports_live_counts_matching_enforcement(
        self, quota_db, monkeypatch
    ):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 3)
        monkeypatch.setattr(settings, "quota_default_max_environments", 5)
        await _add_workspace("t1", "a")
        await _add_workspace("t1", "b")
        # Archived rows bind nothing, so they must not consume budget here
        # either — the same rule the environment check applies.
        await _add_environment("t1", "dev", "active")
        await _add_environment("t1", "old", "archived")

        overview = await get_quota_service().overview_for("t1")
        by_resource = {item.resource: item for item in overview.resources}

        assert (by_resource["workspace"].current, by_resource["workspace"].limit) == (2, 3)
        assert (
            by_resource["environment"].current,
            by_resource["environment"].limit,
        ) == (1, 5)
        assert not by_resource["workspace"].exhausted

    async def test_exhausted_matches_what_the_check_would_reject(
        self, quota_db, monkeypatch
    ):
        """The flag and the 429 must never disagree."""
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 1)
        await _add_workspace("t1", "a")

        overview = await get_quota_service().overview_for("t1")
        workspace = next(
            item for item in overview.resources if item.resource == "workspace"
        )

        assert workspace.exhausted is True
        with pytest.raises(QuotaExceededError):
            await get_quota_service().check_workspace_creation("t1")

    async def test_row_overrides_and_zero_is_an_explicit_ban(
        self, quota_db, monkeypatch
    ):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 50)
        async with database.get_session() as session:
            session.add(TenantQuotaModel(tenant_id="t1", max_workspaces=0))

        overview = await get_quota_service().overview_for("t1")
        workspace = next(
            item for item in overview.resources if item.resource == "workspace"
        )

        assert workspace.limit == 0
        # Zero usage against a zero ceiling is still exhausted: the tenant is
        # banned from creating, and the UI must say so rather than show 0/0 as
        # "plenty of room".
        assert workspace.exhausted is True

    async def test_lease_usage_comes_from_the_snapshot(self, quota_db, monkeypatch):
        """Leases are Redis-side state; the snapshot is the control plane's view."""
        monkeypatch.setattr(settings, "quota_default_max_concurrent_leases", 10)
        await get_quota_service().apply_lease_usage({"t1": 7})

        overview = await get_quota_service().overview_for("t1")
        leases = next(
            item for item in overview.resources if item.resource == "concurrent_leases"
        )

        assert (leases.current, leases.limit) == (7, 10)
        assert overview.peak_concurrent_leases == 7

    async def test_never_observed_leases_are_not_reported_as_zero(self, quota_db):
        """"The capacity loop has not run" differs from "measured zero".

        Reporting 0 would claim a measurement the platform never made — and on a
        deployment with the capacity controller disabled, that claim would be
        permanent.
        """
        overview = await get_quota_service().overview_for("t1")
        leases = next(
            item for item in overview.resources if item.resource == "concurrent_leases"
        )

        assert leases.current is None
        assert leases.exhausted is False

    async def test_runtime_replicas_report_a_limit_without_a_count(self, quota_db):
        """Occupancy is a placement-time property of the heartbeat stream.

        The control plane has no honest instantaneous reading of it, so it
        advertises the ceiling and declines to invent the usage.
        """
        overview = await get_quota_service().overview_for("t1")
        replicas = next(
            item for item in overview.resources if item.resource == "runtime_replicas"
        )

        assert replicas.current is None
        assert replicas.limit > 0

    async def test_unreadable_counters_yield_unknown_not_zero(
        self, quota_db, monkeypatch
    ):
        """Pairs with the fail-open enforcement: both must degrade the same way.

        If the counter is unreadable the check is skipped (creates are allowed),
        so reporting 0 here would tell the user they have their entire budget
        free at the one moment the platform cannot count it.
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _broken_session():
            raise RuntimeError("database is unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr("src.quota.service.get_session", _broken_session)
        overview = await get_quota_service().overview_for("t1")

        assert all(item.current is None for item in overview.resources)
        assert all(not item.exhausted for item in overview.resources)


class _FakeRedis:
    """Records SETs only — the publisher performs no other operation."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.written[key] = value
        return True


class TestQuotaPublisher:
    async def test_publishes_defaults_plus_deviant_rows_only(self, quota_db, monkeypatch):
        from datacode_common.lease import (
            decode_quota_snapshot,
            quota_defaults_redis_key,
            tenant_quota_redis_key,
        )

        from src.config import settings as cfg
        from src.quota.publisher import publish_quotas

        monkeypatch.setattr(cfg, "quota_default_max_concurrent_leases", 64)
        monkeypatch.setattr(cfg, "quota_default_max_runtime_replicas", 4)

        async with database.get_session() as session:
            # Deviates on leases → published; equals the defaults → skipped.
            session.add(TenantQuotaModel(tenant_id="t-big", max_concurrent_leases=999))
            session.add(TenantQuotaModel(tenant_id="t-normal"))

        client = _FakeRedis()
        written = await publish_quotas(client)

        assert written == 2  # defaults + one override
        defaults = decode_quota_snapshot(client.written[quota_defaults_redis_key()])
        assert defaults is not None
        assert defaults.max_concurrent_leases == 64
        assert defaults.max_runtime_replicas == 4

        override = decode_quota_snapshot(
            client.written[tenant_quota_redis_key("t-big")]
        )
        assert override is not None
        assert override.max_concurrent_leases == 999
        assert override.max_runtime_replicas == 4
        assert tenant_quota_redis_key("t-normal") not in client.written
