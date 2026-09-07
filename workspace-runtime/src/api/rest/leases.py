"""Lease placement endpoints — the gateway's acquire channel (design 06 P4a).

These are INTERNAL endpoints (shared-secret guarded, never user traffic): they
exist so the gateway can turn "no lease found" into "lease held, route here".

| Method | Path                            | Purpose                             |
|--------|---------------------------------|-------------------------------------|
| POST   | ``/internal/leases/acquire``    | Place a lease on the best instance  |
| POST   | ``/internal/leases/accept``     | Instance-side: take a placed lease  |
| POST   | ``/internal/leases/release``    | Release a lease this holder owns    |

``acquire`` may be load balanced to ANY runtime replica; that replica runs
placement and either serves locally or forwards to the winner via ``accept``.
``accept`` is never called through the gateway — it is instance-to-instance,
which is why it must tolerate being the target of a placement decision made
elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.dagster.internal_auth import InternalCaller
from src.lease.holder import LeaseSplitBrain, LeaseUnavailable
from src.lease.manager import LeaseFullError
from src.lease.server import LeaseAcquireError, LeaseServer
from src.utils.errors import ErrorCode, ServiceError
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/leases", tags=["internal-leases"])

# Retry-After advertised with LEASE_UNAVAILABLE (429). A lease frees when its
# holder drains or its TTL lapses; one heartbeat interval plus margin is the
# earliest that can happen, so a shorter window would invite a retry storm
# against a verdict that cannot have changed.
_LEASE_RETRY_AFTER_SECONDS = "30"


class LeaseRequest(BaseModel):
    """One working copy, identified the same way everywhere."""

    lease_key: str = Field(min_length=5, max_length=512)


class AcquireResponse(BaseModel):
    runtime_id: str
    addr: str


def _server() -> LeaseServer:
    """The running lease server, or a 503 explaining its absence.

    The lease subsystem being off is a deployment state the gateway must be able
    to distinguish from a bug: without this mapping, every acquire would look
    like an internal error and the gateway's degradation logic (which keys on
    errors from the acquirer) could not tell "unconfigured" from "broken".
    """
    from src.lease.wiring import get_lease_server

    server = get_lease_server()
    if server is None:
        raise ServiceError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "The lease subsystem is not running on this instance "
            "(disabled, or no internal token configured)",
            status_code=503,
        )
    return server


@router.post("/acquire", response_model=AcquireResponse)
async def acquire(req: LeaseRequest, _: InternalCaller) -> AcquireResponse:
    """Place a lease and return where the gateway should route.

    Returns before materialization completes: the first forwarded request for
    the working copy pays the remaining cost, which is the design's documented
    ``202 {warmup: in_progress}`` behaviour — blocking here would block the
    gateway request that triggered the placement.
    """
    server = _server()
    try:
        result = await server.acquire(req.lease_key)
    except LeaseAcquireError as exc:
        # Split brain (design 08 §7): the file lock and the lease disagree on
        # the owner — the shape a Redis degradation produces. The contract
        # answer is 409 with a caller retry, NOT a capacity back-off: retrying
        # immediately is safe (the arbitration resolves within one attempt)
        # and the condition is not about fleet capacity at all.
        if isinstance(exc.__cause__, LeaseSplitBrain):
            raise ServiceError(
                ErrorCode.LEASE_SPLIT_BRAIN,
                (
                    "Lease ownership and the working copy's file lock "
                    "disagree; retrying resolves the arbitration"
                ),
                detail=str(exc),
                status_code=409,
            ) from exc
        # No instance could take it: the contract answer is 429
        # LEASE_UNAVAILABLE with Retry-After (design 08 §7). This is a
        # capacity verdict, not a transport failure — the gateway must NOT
        # degrade to its stable-hash path here, because that would route
        # around the very capacity decision this endpoint just made.
        raise ServiceError(
            ErrorCode.LEASE_UNAVAILABLE,
            (
                "No runtime instance can take this working copy right now; "
                "retry after the window below"
            ),
            detail=str(exc),
            status_code=429,
            headers={"Retry-After": _LEASE_RETRY_AFTER_SECONDS},
        ) from exc
    return AcquireResponse(**result)


@router.post("/accept", response_model=AcquireResponse)
async def accept(req: LeaseRequest, _: InternalCaller) -> AcquireResponse:
    """Take a lease another instance placed here.

    Called instance-to-instance only. The lease-taking handshake (SET NX +
    flock) runs synchronously — by the time this answers, the lease is
    committed — while the materialization it enables continues in the
    background: the placing instance needs the ADDRESS, not the files.
    """
    server = _server()
    try:
        await server.adopt_locally(req.lease_key)
    except LeaseUnavailable as exc:
        # The lease raced to another instance between placement and this call.
        # 409 tells the placer to re-place with this instance excluded.
        logger.info("lease_accept_refused", lease_key=req.lease_key, detail=str(exc))
        raise ServiceError(
            ErrorCode.CONFLICT,
            f"This instance cannot take the lease: {exc}",
            status_code=409,
        ) from exc
    except LeaseFullError as exc:
        # Placement read a stale heartbeat. Also 409: re-place elsewhere.
        logger.info("lease_accept_full", lease_key=req.lease_key, detail=str(exc))
        raise ServiceError(
            ErrorCode.CONFLICT,
            f"This instance is at capacity: {exc}",
            status_code=409,
        ) from exc
    return AcquireResponse(runtime_id=server.holder_runtime_id, addr=server.addr)


@router.post("/release")
async def release(req: LeaseRequest, _: InternalCaller) -> dict:
    """Release a lease. Idempotent; releasing an unheld lease succeeds.

    The gateway calls this on the failure path, where the right semantics are
    "make sure it is gone", not "assert it was there".
    """
    result = await _server().release(req.lease_key)
    return result


@router.post("/drain")
async def drain_instance(_: InternalCaller) -> dict:
    """Begin a graceful drain of this instance (P8 capacity orchestration).

    Called by control-service's LeaseController on the instance it chose as a
    scale-down victim: the draining flag becomes visible in the heartbeat
    synchronously, while the (potentially slow) final snapshots and lease
    releases continue in the background. The controller polls the heartbeat
    until the instance disappears, then shrinks the replica count.
    """
    from src.lease.wiring import begin_controller_drain

    return await begin_controller_drain()
