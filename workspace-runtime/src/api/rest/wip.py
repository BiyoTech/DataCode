"""WIP durability-contract endpoints (design 07 Q6 / ADR-003).

The contract published to users — "a non-graceful crash loses at most
``wip_snapshot_interval_seconds`` of uncommitted work" — is only honest if the
risk window is VISIBLE. These endpoints are that visibility:

| Method | Path                    | Purpose                                        |
|--------|-------------------------|------------------------------------------------|
| GET    | ``/api/v1/wip/status``  | "Last protected at" indicator + pending prompt |
| POST   | ``/api/v1/wip/recover`` | Restore a pending snapshot (user decision)    |
| POST   | ``/api/v1/wip/discard`` | Drop a pending snapshot (user decision)        |

Design notes:

* ``status`` answers ``{enabled: false}`` when the lease subsystem is off. The
  frontend then hides the WIP UI entirely instead of showing a broken or falsely
  reassuring indicator — a user who believes they are protected, and is not, is
  the exact failure mode the contract forbids.
* recovering or discarding a snapshot is strictly the USER's decision (design 07
  Q6): applying a snapshot automatically would resurrect edits someone
  deliberately abandoned, and could conflict with work done since.
* ownership is enforced here, not only at the gateway: a WIP snapshot protects
  ONE user's uncommitted work, so only that user may recover or drop it. The
  caller's identity arrives on gateway-controlled headers and cannot be forged.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.deps import Caller, WorkspacePath
from src.api.models.requests import WorkspacePathMixin
from src.config import settings
from src.utils.errors import ErrorCode, PermissionDeniedError, ServiceError
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/wip", tags=["wip"])


class WipActionRequest(WorkspacePathMixin):
    """Address one working copy, the same way every other endpoint does."""


class PendingSnapshot(BaseModel):
    """A snapshot found on the Git server that the user has not decided on."""

    ref: str
    commit: str
    # UTC ISO-8601, from the snapshot commit's committer timestamp; null when
    # the commit could not be read (the prompt then omits the age hint).
    taken_at: Optional[str] = None


class WipStatus(BaseModel):
    """Protection state of one working copy, driving the frontend indicator."""

    # False while the lease subsystem is off: no WIP machinery is running, and
    # the frontend must hide the indicator rather than show a wrong one.
    enabled: bool
    # False when the latest snapshot attempt failed while the copy was dirty —
    # this is the "your changes are NOT protected" degradation banner.
    protected: bool = True
    # UTC ISO-8601 of the last successful snapshot, null when none was taken.
    last_protected_at: Optional[str] = None
    # Cadence the snapshot machinery runs at; the frontend polls at half this.
    snapshot_interval_seconds: int = 0
    pending_snapshot: Optional[PendingSnapshot] = None


def _lease_key_for(workspace_path: str) -> str:
    """The lease key a validated working-copy path maps to."""
    from src.workspace.identity import resolve_workspace_identity

    return resolve_workspace_identity(workspace_path).lease_key


def _assert_owner(caller_user_id: str, workspace_path: str) -> None:
    """A WIP snapshot belongs to one user; only that user may act on it.

    The gateway already authorizes workspace-scoped access; this is the finer
    rule — members can read each other's code, but nobody else recovers or
    discards MY uncommitted work.
    """
    from src.workspace.identity import resolve_workspace_identity

    owner = resolve_workspace_identity(workspace_path).user_id
    if owner != caller_user_id:
        raise PermissionDeniedError(
            "Only the working copy's owner may view or change its "
            "WIP protection state"
        )


def _require_served(lease_key: str):
    """The manager and served copy for ``lease_key``, or a structured error.

    Returns ``(manager, served)``. Raises 409 when this instance is not the
    holder: the request as routed is unfulfillable and the caller (or gateway)
    must retry against the real holder — same semantics as the internal
    accept endpoint.
    """
    from src.lease.wiring import get_lease_manager

    manager = get_lease_manager()
    if manager is None:
        raise ServiceError(
            ErrorCode.CONFLICT,
            "WIP protection is not enabled on this deployment "
            "(lease subsystem off); there is nothing to recover or discard",
            status_code=409,
        )
    served = manager.served_copy(lease_key)
    if served is None:
        logger.info("wip_api_not_served_here", lease_key=lease_key)
        raise ServiceError(
            ErrorCode.CONFLICT,
            "This instance is not serving the working copy; retry shortly "
            "to be routed to its current holder",
            status_code=409,
        )
    return manager, served


@router.get("/status", response_model=WipStatus)
async def wip_status(workspace_path: WorkspacePath, caller: Caller) -> WipStatus:
    """Protection state of one working copy, for the editor indicator."""
    from src.lease.wiring import get_lease_manager

    _assert_owner(caller.user_id, workspace_path)

    interval = settings.wip_snapshot_interval_seconds
    manager = get_lease_manager()
    if manager is None:
        return WipStatus(enabled=False, snapshot_interval_seconds=interval)

    lease_key = _lease_key_for(workspace_path)
    state = manager.copy_protection_status(lease_key)
    if state is None:
        # Routed here without this instance serving the copy: the lease moved
        # or the gateway's route cache is stale. 503 (not 409) so the polling
        # frontend treats it as transient instead of surfacing an error banner
        # the user cannot act on.
        raise ServiceError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "This instance is not serving the working copy right now; "
            "the next request will be routed to its owner",
            status_code=503,
        )

    pending = state["pending_snapshot"]
    last = state["last_protected_at"]
    return WipStatus(
        enabled=True,
        protected=state["protected"],
        last_protected_at=last.isoformat() if last else None,
        snapshot_interval_seconds=interval,
        pending_snapshot=(
            PendingSnapshot(
                ref=pending.ref,
                commit=pending.commit,
                taken_at=(
                    pending.taken_at.isoformat() if pending.taken_at else None
                ),
            )
            if pending is not None
            else None
        ),
    )


@router.post("/recover")
async def recover_snapshot(req: WipActionRequest, caller: Caller) -> dict:
    """Restore the pending snapshot into the working copy (user decision)."""
    _assert_owner(caller.user_id, req.workspace_path)

    lease_key = _lease_key_for(req.workspace_path)
    manager, served = _require_served(lease_key)
    if served.pending_snapshot is None:
        # Idempotent answer: the decision was already made (or never needed).
        return {"recovered": False, "reason": "no_pending_snapshot"}

    applied = await manager.recover_snapshot(lease_key)
    if not applied:
        # Either the copy grew uncommitted changes since materialization, or
        # the apply itself conflicted; both need a human with a working tree,
        # not a blind retry.
        raise ServiceError(
            ErrorCode.CONFLICT,
            "The snapshot could not be applied: the working copy has "
            "uncommitted changes or the apply conflicted. Commit or stash "
            "them first, then retry.",
            status_code=409,
        )
    logger.info(
        "wip_recovered_via_api",
        workspace_path=req.workspace_path,
        lease_key=lease_key,
    )
    return {"recovered": True}


@router.post("/discard")
async def discard_snapshot(req: WipActionRequest, caller: Caller) -> dict:
    """Drop the pending snapshot from the Git server (user decision)."""
    _assert_owner(caller.user_id, req.workspace_path)

    lease_key = _lease_key_for(req.workspace_path)
    manager, served = _require_served(lease_key)
    if served.pending_snapshot is None:
        # Idempotent: dropping a snapshot that is already gone is what the
        # user asked for, not an error.
        return {"discarded": False, "reason": "no_pending_snapshot"}

    discarded = await manager.discard_snapshot(lease_key)
    if not discarded:
        # The remote ref deletion failed (Git server unreachable). The prompt
        # stays up and the user can retry; answering success here would make a
        # stale snapshot reappear at the next materialization.
        raise ServiceError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "The snapshot could not be removed from the Git server; "
            "please retry in a moment",
            status_code=503,
        )
    logger.info(
        "wip_discarded_via_api",
        workspace_path=req.workspace_path,
        lease_key=lease_key,
    )
    return {"discarded": True}
