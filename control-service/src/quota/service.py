"""Tenant quota enforcement (design 06 P8 — the implementation half).

The schema for ``tenant_quotas`` / ``tenant_usage`` landed with P5; this module
is the enforcement that compares live usage against the limits and rejects the
request with 429 + ``Retry-After`` when a tenant has exhausted one.

Design decisions worth pinning
------------------------------
**Live COUNT, snapshot on the side.** The enforcement check counts the source
tables directly (one indexed COUNT on a creation path — not hot) instead of
trusting the ``tenant_usage`` counters. A counter that missed a rollback, a
reaper destroy or an archive-all would either strand a tenant below its real
usage or let it silently exceed the ceiling; the COUNT cannot drift. The
``tenant_usage`` row is still refreshed at every check so read paths
(observability, the capacity loop) see a denormalized snapshot without
aggregating the registry themselves — which is the role its P5 docstring
assigns it.

**Leases are not counted here.** Lease admission is the hottest of the four
resources (it runs on the gateway's acquire path), and its live state lives in
Redis heartbeats, not in this database. The lease quota is therefore enforced
by workspace-runtime from the heartbeat stream (see ``src.lease.quotas`` there)
and only *reported* into ``tenant_usage.concurrent_leases`` by the capacity
loop. Enforcing it here would have put a synchronous DB round-trip in front of
every placement.

**Fail-open, loudly.** Quota exhaustion is a capacity problem, not a security
boundary: a database hiccup must not turn into "no tenant can create anything".
When the counters cannot be read the check is skipped and a warning is logged;
the next successful check re-syncs the snapshot.

**Defaults resolve here, not in the schema.** A missing ``tenant_quotas`` row
means "the platform default applies" (the P5 migration documents this), so the
defaults live in control-service settings where operators can tune them per
deployment.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from datacode_common import naming
from sqlalchemy import func, select

from src.config import settings
from src.quota.models import TenantQuotaModel, TenantUsageModel
from src.utils.errors import QuotaExceededError
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import WorkspaceModel

logger = get_logger(__name__)

# Environment statuses whose row still holds live resources. ``archived`` and
# ``failed`` rows recycle their name but bind nothing, so they must not consume
# the tenant's environment budget — otherwise a destroyed-and-recreated sandbox
# would count twice.
LIVE_ENVIRONMENT_STATUSES = (
    naming.STATUS_CREATING,
    naming.STATUS_ACTIVE,
    naming.STATUS_APPLYING,
    naming.STATUS_PROMOTING,
    naming.STATUS_DRIFTED,
    naming.STATUS_ARCHIVING,
)


@dataclass(frozen=True)
class QuotaLimits:
    """The ceilings that apply to one tenant, defaults already resolved."""

    tenant_id: str
    max_workspaces: int
    max_environments: int
    max_concurrent_leases: int
    max_runtime_replicas: int


@dataclass(frozen=True)
class ResourceUsage:
    """One resource's ceiling next to what the tenant is using.

    ``current`` is ``None`` when the live count could not be read — the same
    honesty rule the enforcement path follows (it fails open). A UI showing 0/50
    for an unreadable counter would tell the user they have their whole budget
    free at the exact moment the platform cannot tell.
    """

    resource: str
    limit: int
    current: t.Optional[int]

    @property
    def exhausted(self) -> bool:
        """Whether a creation attempt would be rejected right now."""
        return self.current is not None and self.current >= self.limit


@dataclass(frozen=True)
class QuotaOverview:
    """Everything ``GET /api/v1/quotas`` reports about one tenant."""

    tenant_id: str
    resources: t.Tuple[ResourceUsage, ...]
    # Watermark carried straight from tenant_usage: it is capacity-planning
    # input the live counts cannot express (a tenant at 3/64 leases now may have
    # peaked at 60 this morning).
    peak_concurrent_leases: int


class QuotaService:
    """Reads limits, counts live usage, rejects exhausted tenants (429)."""

    async def limits_for(self, tenant_id: str) -> QuotaLimits:
        """Resolve one tenant's limits: its row, else the platform defaults.

        An unreadable ``tenant_quotas`` table degrades to the DEFAULTS rather
        than raising. That is the same fail-open stance the checks take, and it
        is the safe direction here too: defaults are the platform's declared
        policy, so a database hiccup relaxes a tenant's custom ceiling for the
        duration of the outage instead of locking it out entirely.
        """
        try:
            async with get_session() as session:
                row = await session.get(TenantQuotaModel, tenant_id)
        except Exception as exc:  # noqa: BLE001 — capacity, not security
            logger.warning(
                "quota_limits_read_failed",
                tenant_id=tenant_id,
                error=str(exc),
                effect="the platform defaults apply until the read recovers",
            )
            row = None

        if row is None:
            return QuotaLimits(
                tenant_id=tenant_id,
                max_workspaces=settings.quota_default_max_workspaces,
                max_environments=settings.quota_default_max_environments,
                max_concurrent_leases=settings.quota_default_max_concurrent_leases,
                max_runtime_replicas=settings.quota_default_max_runtime_replicas,
            )
        return QuotaLimits(
            tenant_id=tenant_id,
            max_workspaces=self._value(
                row.max_workspaces, settings.quota_default_max_workspaces
            ),
            max_environments=self._value(
                row.max_environments, settings.quota_default_max_environments
            ),
            max_concurrent_leases=self._value(
                row.max_concurrent_leases, settings.quota_default_max_concurrent_leases
            ),
            max_runtime_replicas=self._value(
                row.max_runtime_replicas, settings.quota_default_max_runtime_replicas
            ),
        )

    async def overview_for(self, tenant_id: str) -> QuotaOverview:
        """Report one tenant's ceilings next to its usage (the read endpoint).

        Where each number comes from, and why it differs per resource:

        * workspaces / environments — LIVE COUNT, the very same query the
          enforcement path runs. Anything else could show a user "12/50" while
          their next create is rejected, which is worse than showing nothing;
        * leases — the ``tenant_usage`` snapshot, because the live state is Redis
          heartbeats on the runtime side (see the module docstring). It is
          refreshed by the capacity loop, so a deployment with that loop disabled
          reports the last observed value rather than a fabricated zero;
        * runtime replicas — reported as a LIMIT with no current value: the
          occupancy is a placement-time property of the heartbeat stream, and the
          control plane has no honest instantaneous reading of it.

        An unreadable counter yields ``None`` rather than 0 — the enforcement
        path fails open on the same condition, and a UI must not claim a full
        budget at the moment the platform cannot count.
        """
        limits = await self.limits_for(tenant_id)
        live_workspaces = await self._count_live_workspaces(tenant_id)
        live_environments = await self._count_live_environments(tenant_id)
        usage = await self._usage_snapshot(tenant_id)

        resources = (
            ResourceUsage("workspace", limits.max_workspaces, live_workspaces),
            ResourceUsage("environment", limits.max_environments, live_environments),
            ResourceUsage(
                "concurrent_leases",
                limits.max_concurrent_leases,
                usage.concurrent_leases if usage is not None else None,
            ),
            ResourceUsage("runtime_replicas", limits.max_runtime_replicas, None),
        )
        return QuotaOverview(
            tenant_id=tenant_id,
            resources=resources,
            peak_concurrent_leases=(
                usage.peak_concurrent_leases if usage is not None else 0
            ),
        )

    async def check_workspace_creation(self, tenant_id: str) -> None:
        """Reject (429) when the tenant already owns its ceiling of workspaces.

        Called at the very top of the workspace-create flow, before any row,
        branch or scaffold is built, so a rejected request leaves no cleanup.
        """
        live = await self._count_live_workspaces(tenant_id)
        if live is None:
            return  # fail-open: see module docstring
        limits = await self.limits_for(tenant_id)
        if live >= limits.max_workspaces:
            raise QuotaExceededError(
                "workspace",
                tenant_id=tenant_id,
                limit=limits.max_workspaces,
                current=live,
                retry_after_seconds=settings.quota_retry_after_seconds,
            )
        await self._refresh_usage(tenant_id, live_workspaces=live)

    async def check_environment_creation(self, tenant_id: str) -> None:
        """Reject (429) when the tenant is at its live-environment ceiling.

        Tenant-wide, complementing the per-workspace cap the registry already
        enforces: a tenant spreading one sandbox over each of its workspaces
        would stay under every per-workspace limit while exhausting the
        platform's branch/deployment capacity.
        """
        live = await self._count_live_environments(tenant_id)
        if live is None:
            return
        limits = await self.limits_for(tenant_id)
        if live >= limits.max_environments:
            raise QuotaExceededError(
                "environment",
                tenant_id=tenant_id,
                limit=limits.max_environments,
                current=live,
                retry_after_seconds=settings.quota_retry_after_seconds,
            )
        await self._refresh_usage(tenant_id, live_environments=live)

    async def apply_lease_usage(self, per_tenant: dict[str, int]) -> None:
        """Record lease usage reported by the capacity loop (heartbeat-derived).

        ``peak_concurrent_leases`` only ever grows here: it is a high-water mark
        for capacity planning, and a transient dip in the live count must not
        erase the maximum the tenant has actually reached.
        """
        if not per_tenant:
            return
        async with get_session() as session:
            for tenant_id, count in per_tenant.items():
                row = await session.get(TenantUsageModel, tenant_id)
                if row is None:
                    session.add(
                        TenantUsageModel(
                            tenant_id=tenant_id,
                            concurrent_leases=count,
                            peak_concurrent_leases=count,
                        )
                    )
                else:
                    row.concurrent_leases = count
                    row.peak_concurrent_leases = max(row.peak_concurrent_leases, count)
            await session.commit()

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _value(row_value: int | None, default: int) -> int:
        """A NULL column falls back to the default (0 stays 0: an explicit ban)."""
        return default if row_value is None else row_value

    async def _count_live_workspaces(self, tenant_id: str) -> int | None:
        try:
            async with get_session() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceModel)
                    .where(
                        WorkspaceModel.tenant_id == tenant_id,
                        WorkspaceModel.status == "active",
                    )
                )
                return int(count or 0)
        except Exception as exc:  # noqa: BLE001 — quota is capacity, not security
            logger.warning(
                "quota_usage_read_failed",
                resource="workspaces",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return None

    async def _count_live_environments(self, tenant_id: str) -> int | None:
        from src.environments.models import EnvironmentModel

        try:
            async with get_session() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(EnvironmentModel)
                    .where(
                        EnvironmentModel.tenant_id == tenant_id,
                        EnvironmentModel.status.in_(LIVE_ENVIRONMENT_STATUSES),
                    )
                )
                return int(count or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "quota_usage_read_failed",
                resource="environments",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return None

    async def _usage_snapshot(self, tenant_id: str) -> t.Optional[TenantUsageModel]:
        """The tenant's denormalized usage row, or ``None`` when absent/unreadable.

        Absent is the normal state of a tenant that has never held a lease, and
        it is reported as ``None`` rather than a zeroed row so the caller can tell
        "never observed" from "observed as zero" — the first means the capacity
        loop has not run, the second is a real measurement.
        """
        try:
            async with get_session() as session:
                return await session.get(TenantUsageModel, tenant_id)
        except Exception as exc:  # noqa: BLE001 — a read model, never critical
            logger.warning(
                "quota_usage_snapshot_read_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return None

    async def _refresh_usage(
        self,
        tenant_id: str,
        *,
        live_workspaces: int | None = None,
        live_environments: int | None = None,
    ) -> None:
        """Upsert the denormalized snapshot; failures only cost the snapshot.

        Last-writer-wins is acceptable: the row is a read model refreshed on
        every enforcement check, never the input to one.
        """
        try:
            async with get_session() as session:
                row = await session.get(TenantUsageModel, tenant_id)
                if row is None:
                    session.add(
                        TenantUsageModel(
                            tenant_id=tenant_id,
                            live_workspaces=live_workspaces or 0,
                            live_environments=live_environments or 0,
                        )
                    )
                else:
                    if live_workspaces is not None:
                        row.live_workspaces = live_workspaces
                    if live_environments is not None:
                        row.live_environments = live_environments
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "quota_usage_snapshot_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )


_service: QuotaService | None = None


def get_quota_service() -> QuotaService:
    """Return the shared quota service instance."""
    global _service
    if _service is None:
        _service = QuotaService()
    return _service
