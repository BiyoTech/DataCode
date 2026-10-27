"""Tenant quota bookkeeping (design 03).

``tenant_quotas`` carries the LIMITS the platform enforces on a tenant;
``tenant_usage`` carries the LIVE counters those limits are compared against.
The rules that read them live in :mod:`src.quota.service`; this module is only
the contract both sides compile against.

All timestamps are stored in UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String

from src.workspace.models import Base


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class TenantQuotaModel(Base):
    """Per-tenant resource limits.

    One row per tenant. Values are the CAPACITY the tenant may consume; a
    missing row means the platform default applies (the enforcement layer
    resolves defaults, not the schema).
    """

    __tablename__ = "tenant_quotas"

    tenant_id = Column(String(64), primary_key=True)
    # Ceiling on concurrently-held workspace leases across the tenant's users.
    max_concurrent_leases = Column(Integer, nullable=False, default=64)
    # Ceiling on live (non-archived) environments across the tenant's
    # workspaces; the per-workspace cap lives on the registry path.
    max_environments = Column(Integer, nullable=False, default=500)
    # Ceiling on workspaces the tenant may own.
    max_workspaces = Column(Integer, nullable=False, default=50)
    # Ceiling on workspace-runtime replicas (cells) the tenant may occupy.
    max_runtime_replicas = Column(Integer, nullable=False, default=4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("idx_tenant_quota_tenant", "tenant_id"),
    )


class TenantUsageModel(Base):
    """Per-tenant live usage counters, maintained by the control plane.

    Counters are DENORMALIZED on purpose: reconciliation reads one row instead
    of aggregating the whole registry. Updated by the flows that change them
    (environment lifecycle, workspace lifecycle, lease placement reports).
    """

    __tablename__ = "tenant_usage"

    tenant_id = Column(String(64), primary_key=True)
    live_environments = Column(Integer, nullable=False, default=0)
    live_workspaces = Column(Integer, nullable=False, default=0)
    concurrent_leases = Column(Integer, nullable=False, default=0)
    # Highest watermark observed since the last reset; capacity planning input.
    peak_concurrent_leases = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("idx_tenant_usage_tenant", "tenant_id"),
    )
