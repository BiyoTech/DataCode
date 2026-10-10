"""Environment registry writes the RUNTIME asks the control plane to perform.

Why this surface exists
-----------------------
``environments`` and ``environment_events`` belong to the control plane: it
creates them in the migrate step and owns their lifecycle. The runtime used to
write two of their columns directly — the applied watermark after a successful
apply, and a ``merge`` audit row — because both facts originate on the replica
that holds the working copy.

Two services writing one table is the shape of every ownership bug this
platform has already paid for (the ``create_all`` race, the reclaim timestamp
that was fixed on one side only). Reads are a different matter: they are on the
hot path, tolerate a stale row, and cannot corrupt anything — those stay
direct. Writes come here.

Only facts the runtime is the sole author of are accepted. There is no generic
"update this row" endpoint: the control plane applies the domain rule
(a whitelist of columns, the event schema) rather than relaying whatever the
caller sent, so this channel cannot become a way around the registry's own
invariants.

Guarded by the same ``X-Internal-Token`` the control plane presents when it
calls the runtime: one trust relationship between the same two services, so a
second secret would be two things to rotate and one more to get wrong.

NOTE: this module deliberately omits ``from __future__ import annotations``.
The request models below carry ``Optional[...]`` fields, and pydantic resolves
those annotations at class-definition time; stringified, it cannot see the
names and the model is left "not fully defined".
"""

import typing as t
from datetime import datetime, timezone

from datacode_common.security.internal_token import (
    INTERNAL_TOKEN_HEADER,
    extract_token,
    token_matches,
)
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from src.config import settings
from src.environments.repository import EnvironmentRepository
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/env-registry", tags=["env-registry"])


def _require_internal_token(
    x_internal_token: str = Header(default="", alias=INTERNAL_TOKEN_HEADER),
    authorization: str = Header(default=""),
) -> None:
    """Fail closed; constant-time comparison (see cell_migration for the why)."""
    expected = settings.runtime_internal_token
    if not expected:
        logger.warning("env_registry_disabled_request_rejected")
        raise HTTPException(status_code=401, detail="internal token required")

    presented = extract_token(
        internal_header=x_internal_token, authorization=authorization
    )
    if not token_matches(expected, presented):
        logger.warning("env_registry_token_rejected", presented=bool(presented))
        raise HTTPException(status_code=401, detail="internal token required")


class AppliedMarkRequest(BaseModel):
    """The watermark a successful apply leaves on its environment."""

    environment_id: str = Field(min_length=1, max_length=256)
    commit_hash: str = ""
    # The apply ran from a working copy with uncommitted changes, so its data
    # is NOT reproducible from commit_hash. Stored rather than hidden: the UI
    # turns it into drift that a commit + re-apply clears.
    dirty: bool = False
    applied_at: t.Optional[datetime] = None


@router.post("/applied", dependencies=[Depends(_require_internal_token)])
async def mark_applied(req: AppliedMarkRequest) -> dict:
    """Record a successful apply on the registry row.

    Returns ``{"updated": false}`` for an unknown environment rather than 404:
    the caller treats this as bookkeeping that must never fail the apply it
    describes, and a missing row is not something it could act on.
    """
    updated = await EnvironmentRepository().update(
        req.environment_id,
        last_applied_commit=req.commit_hash or None,
        last_applied_at=req.applied_at or datetime.now(timezone.utc),
        last_applied_dirty=1 if req.dirty else 0,
    )
    logger.info(
        "environment_applied_marked",
        environment_id=req.environment_id,
        commit_hash=req.commit_hash,
        dirty=req.dirty,
        updated=updated,
    )
    return {"updated": bool(updated)}


class EventRequest(BaseModel):
    """One lifecycle event the runtime observed."""

    environment_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=64)
    actor_user_id: str = ""
    trigger_type: str = "human"
    succeeded: bool = True
    commit_hash: str = ""
    detail: t.Optional[dict] = None
    duration_ms: int = 0


@router.post("/events", dependencies=[Depends(_require_internal_token)])
async def record_event(req: EventRequest) -> dict:
    """Append one lifecycle event to an environment's audit trail."""
    event_id = await EnvironmentRepository().record_event(
        environment_id=req.environment_id,
        action=req.action,
        actor_user_id=req.actor_user_id,
        trigger_type=req.trigger_type,
        succeeded=req.succeeded,
        commit_hash=req.commit_hash,
        detail=req.detail,
        duration_ms=req.duration_ms,
    )
    return {"event_id": event_id}
