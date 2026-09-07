"""Quota publisher — pushes lease-relevant limits into Redis (design 06 P8).

Why a publisher at all
----------------------
The lease-acquire path runs on workspace-runtime, on the gateway's request
path, and must stay synchronous-fast. Quota authority (``tenant_quotas``)
lives in the control plane's database. Rather than adding a database round
trip in front of every placement, the control plane PUBLISHES the
lease-relevant slice of every quota to Redis, where the acquire path reads it
back next to the heartbeats it already reads.

Degradation is deliberately fail-open: keys carry a TTL refreshed by the
capacity loop, so a control plane that dies stops enforcing quotas after one
TTL instead of freezing every tenant at its last limits. Quota is capacity
governance; it must not become a hard dependency of lease acquisition.

Key layout (shared with the runtime via ``datacode_common.lease``):

* ``quota:defaults``      — the platform defaults, applied to tenants without
                            a row;
* ``quota:{tenant_id}``   — one tenant's overrides, written only when they
                            differ from the defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone

from datacode_common.lease import (
    TenantQuotaSnapshot,
    encode_quota_snapshot,
    quota_defaults_redis_key,
    tenant_quota_redis_key,
)
from sqlalchemy import select

from src.config import settings
from src.quota.models import TenantQuotaModel
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)


def _now() -> datetime:
    """UTC, always: the platform stores and compares UTC everywhere."""
    return datetime.now(timezone.utc)


def _share_or_none(value: int) -> int | None:
    """0 means "no fairness term" and is published as null.

    Publishing 0 verbatim would make an unset default indistinguishable from a
    deliberate zero on the wire; null keeps the reader's fallback honest
    (absent = spread-only) while still allowing a future explicit override.
    """
    return value if value > 0 else None


def _lease_relevant(quota: TenantQuotaModel) -> tuple[int | None, int | None]:
    """The lease-relevant slice of one quota row, ``None`` meaning unlimited."""
    return (
        None if quota.max_concurrent_leases is None else quota.max_concurrent_leases,
        None if quota.max_runtime_replicas is None else quota.max_runtime_replicas,
    )


async def publish_quotas(client) -> int:
    """Snapshot every lease-relevant quota into Redis.

    Returns the number of keys written (defaults + overrides) for the capacity
    loop's log line. Failures propagate to the caller, which decides whether
    one failed tick is fatal (it is not: the TTL outlives several intervals).
    """
    now = _now()
    defaults = TenantQuotaSnapshot(
        max_concurrent_leases=settings.quota_default_max_concurrent_leases,
        max_runtime_replicas=settings.quota_default_max_runtime_replicas,
        updated_at=now,
        tenant_lease_share=_share_or_none(
            settings.quota_default_tenant_lease_share
        ),
    )
    ttl = max(1, int(settings.quota_publish_ttl_seconds))

    written = 0
    async with get_session() as session:
        await client.set(
            quota_defaults_redis_key(),
            encode_quota_snapshot(defaults),
            ex=ttl,
        )
        written += 1

        rows = (
            (await session.execute(select(TenantQuotaModel))).scalars().all()
        )
        for row in rows:
            leases_limit, replicas_limit = _lease_relevant(row)
            if (leases_limit, replicas_limit) == (
                defaults.max_concurrent_leases,
                defaults.max_runtime_replicas,
            ):
                # Equal to the defaults: the fallback key already covers this
                # tenant, and skipping keeps the keyspace proportional to the
                # number of tenants that actually deviate.
                continue
            snapshot = TenantQuotaSnapshot(
                max_concurrent_leases=leases_limit,
                max_runtime_replicas=replicas_limit,
                updated_at=now,
            )
            await client.set(
                tenant_quota_redis_key(row.tenant_id),
                encode_quota_snapshot(snapshot),
                ex=ttl,
            )
            written += 1

    logger.info(
        "quota_snapshot_published",
        keys=written,
        tenants_with_overrides=written - 1,
        ttl_seconds=ttl,
    )
    return written
