"""Internal code location registration channel (control plane <-> data plane).

## Why this exists

A Code Location gRPC server is ALWAYS local to a dagster-service replica: it
loads user Python code from that replica's filesystem. In production the
DagsterInstance (webserver + daemon + metadata DB) is REMOTE, and it can only
drive a code location it knows the endpoint of. Something has to carry
``{location_name, host, port}`` from every data plane replica to the control
plane.

## Why PULL rather than PUSH

Writing the control plane's ``workspace.yaml`` from here would mean N replicas
concurrently mutating one file — a distributed lock, a shared volume and a
write-conflict story for what is derived state. Instead each replica only
PUBLISHES what it runs, and a single control-plane-side writer polls every
replica and renders the file. One writer, no lock, no shared filesystem between
the planes.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | ``/internal/code-locations``           | What this replica currently serves |
| POST | ``/internal/code-locations/heartbeat`` | Same list + record what the control plane loaded |

The heartbeat is the ACKNOWLEDGEMENT half: the control plane reports back the
revisions it successfully loaded, and this replica marks them confirmed. Without
it a reload that the control plane silently failed to pick up would be
indistinguishable from a healthy one — the assets would just be stale, with
nothing to alarm on. ``pending`` in the response is that alarm.

Both are guarded by the platform's internal token, never by gateway identity:
these are infrastructure facts exchanged between services, not user traffic.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from src.api.dagster.internal_auth import InternalCaller
from src.api.dagster.requests import CodeLocationHeartbeatRequest
from src.api.dagster.responses import (
    CodeLocationAdvertisementInfo,
    CodeLocationRegistry,
)
from src.codeloc.manager import get_code_location_manager
from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/code-locations", tags=["internal"])


async def _registry() -> CodeLocationRegistry:
    """Snapshot of this replica's advertisable code locations.

    Refreshes the vehicle state first: in Pod mode "alive" is the Endpoints
    object behind each location's Service, and telling the control plane about
    a location that has since died would fail its whole workspace load. The
    refresh is a cluster read, which is why this is async.
    """
    manager = get_code_location_manager()
    await manager.refresh_state()
    advertisements = manager.advertisements()
    return CodeLocationRegistry(
        replica_id=manager.replica_id(),
        dagster_mode=settings.dagster_mode,
        generated_at=time.time(),
        locations=[
            CodeLocationAdvertisementInfo(**ad.as_dict()) for ad in advertisements
        ],
        pending=manager.pending_registrations(),
    )


@router.get("", response_model=CodeLocationRegistry)
async def list_code_locations(_: InternalCaller) -> CodeLocationRegistry:
    """Return the code locations the control plane should register.

    Only alive locations with a routable advertised host are listed: an endpoint
    the control plane cannot dial would fail its whole workspace load and take
    down the healthy locations listed alongside it.
    """
    return await _registry()


@router.post("/heartbeat", response_model=CodeLocationRegistry)
async def heartbeat(
    req: CodeLocationHeartbeatRequest, _: InternalCaller
) -> CodeLocationRegistry:
    """Record what the control plane loaded and return the current list.

    Combining acknowledgement and read in one call keeps the control plane's poll
    loop a single round trip, and guarantees the list it receives is at least as
    fresh as the acknowledgement it just sent.

    Locations acknowledged that this replica does not (or no longer) run are
    reported back under ``unknown`` so the control plane can drop them instead of
    dialing a dead endpoint forever.
    """
    unknown = get_code_location_manager().acknowledge(req.acknowledged)
    registry = await _registry()
    registry.unknown = unknown
    logger.info(
        "code_location_heartbeat",
        control_plane=req.control_plane_id,
        acknowledged=len(req.acknowledged),
        unknown=len(unknown),
        serving=len(registry.locations),
        pending=len(registry.pending),
    )
    return registry
