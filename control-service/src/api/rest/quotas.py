"""Quota endpoint — "what am I allowed, and what am I using?" (contract 08 §2).

P8 landed quota ENFORCEMENT: a tenant at its ceiling is rejected with 429 and a
structured payload. What was missing is the other half — a user who hit that
rejection had no way to see their budget, and neither did whoever they asked for
help. This endpoint is that view.

Scope is the caller's own tenant, always
----------------------------------------
The tenant is taken from the gateway-issued identity header, never from a query
parameter. There is deliberately no way to ask about another tenant: quota levels
are commercial information, and an endpoint that accepted a tenant id would be an
enumeration surface guarded only by the correctness of a permission check. The
route therefore needs no permission beyond authentication (contract 08 §6 lists
it as "认证即可"), because the identity IS the scope.

Read-only, and honest about what it could not read
--------------------------------------------------
``current: null`` means the counter was unreadable, which the UI must render as
"unknown" rather than as zero — the enforcement path fails open on exactly the
same condition, so a user seeing 0/50 while creates are being rejected would be
the worst possible pairing.
"""

from __future__ import annotations

import typing as t

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.deps import Caller
from src.quota import get_quota_service
from src.quota.service import QuotaOverview
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/quotas", tags=["quota"])


class QuotaResourceResponse(BaseModel):
    """One resource's ceiling and the tenant's usage of it."""

    resource: str
    limit: int
    # None = the live count could not be read (render as "unknown", never 0).
    current: t.Optional[int] = None
    exhausted: bool = False


class QuotaOverviewResponse(BaseModel):
    """The caller's tenant quota and usage."""

    tenant_id: str
    resources: t.List[QuotaResourceResponse] = Field(default_factory=list)
    peak_concurrent_leases: int = 0
    # Advertised so the UI can word its retry hint with the real number instead
    # of hard-coding one that drifts from the server's Retry-After header.
    retry_after_seconds: int = 60


def _serialize(overview: QuotaOverview, *, retry_after_seconds: int) -> QuotaOverviewResponse:
    return QuotaOverviewResponse(
        tenant_id=overview.tenant_id,
        resources=[
            QuotaResourceResponse(
                resource=item.resource,
                limit=item.limit,
                current=item.current,
                exhausted=item.exhausted,
            )
            for item in overview.resources
        ],
        peak_concurrent_leases=overview.peak_concurrent_leases,
        retry_after_seconds=retry_after_seconds,
    )


@router.get("", response_model=QuotaOverviewResponse)
async def get_my_quotas(caller: Caller) -> QuotaOverviewResponse:
    """Return the calling tenant's quota ceilings and current usage."""
    from src.config import settings

    overview = await get_quota_service().overview_for(caller.tenant_id)
    logger.info(
        "quota_overview_read",
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
        exhausted=[item.resource for item in overview.resources if item.exhausted],
    )
    return _serialize(
        overview, retry_after_seconds=settings.quota_retry_after_seconds
    )
