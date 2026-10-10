"""Cell self-registration — a cell publishes its own address (design 03 §2.4).

The global edge routes a request by reading ``cell_id`` from the token and
looking up that cell's ``endpoint``. Somebody has to put a dialable address in
that column. Two options, and only one of them stays correct:

* an operator fills the table by hand — a second source of truth that drifts the
  first time a cell's address changes, and drifts SILENTLY: routing keeps working
  for tenants already placed and breaks only for new ones;
* **each cell registers itself at startup** — the party that knows the address
  writes it, and a redeploy with a new address self-heals.

This module does the second. Registration itself carries no authorization
decision (unlike the gateway's fail-closed authorization path): it is a startup
side effect that must fail OPEN — a cell whose control plane cannot reach the
global identity service should still serve the tenants already placed on it,
because refusing to start would turn a global outage into a per-cell one.

What a cell may and may not report
----------------------------------
It reports its ``endpoint``, ``region``, ``status`` and ``capacity_tenants`` —
all facts about itself. It does NOT report ``assigned_tenants``: that is derived
from the placement table on the global side, and letting a cell state its own
tenant count would let a stale value distort placement decisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from datacode_common.cell import cell_id
from datacode_common.security.internal_token import auth_headers

# The environment variable that carries identity-service's internal token. The
# cell registrar shares it with (the now gateway-owned) identity callers: one
# secret per service, referenced by name rather than copied.
INTERNAL_TOKEN_ENV = "IDENTITY_SERVICE_INTERNAL_TOKEN"

# Bounded: registration happens during startup, and a hanging global service must
# not hold a cell's boot.
DEFAULT_TIMEOUT_SECONDS = 5.0

# Endpoint a cell advertises, when the deployment cannot derive it. Env-only:
# the address depends on the ingress/Service in front of THIS cell, which no
# shared configuration file can know.
ENV_CELL_ENDPOINT = "DATACODE_CELL_ENDPOINT"
ENV_CELL_REGION = "DATACODE_CELL_REGION"


@dataclass(frozen=True)
class CellRegistration:
    """What a cell publishes about itself."""

    cell_id: str
    endpoint: str
    region: str = ""
    status: str = "active"
    capacity_tenants: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "endpoint": self.endpoint,
            "region": self.region,
            "status": self.status,
            "capacity_tenants": self.capacity_tenants,
        }


def registration_from_env(capacity_tenants: int = 0) -> Optional[CellRegistration]:
    """Build this cell's registration from the environment.

    Returns None when no endpoint is configured, which is the normal state of a
    single-cell install: with one cell there is nothing to route between, so
    registering an address buys nothing and demanding one would make every dev
    machine configure it.
    """
    endpoint = (os.environ.get(ENV_CELL_ENDPOINT) or "").strip()
    if not endpoint:
        return None
    return CellRegistration(
        cell_id=cell_id(),
        endpoint=endpoint,
        region=(os.environ.get(ENV_CELL_REGION) or "").strip(),
        capacity_tenants=capacity_tenants,
    )


async def register_cell(
    identity_url: str,
    registration: CellRegistration,
    *,
    token: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Publish this cell's endpoint to the global registry.

    Returns whether the registration was accepted. Never raises: see the module
    docstring — a cell that cannot reach the global service must still start, and
    the consequence (new tenants are not routed here yet) is recoverable by the
    next restart or a manual call.
    """
    base = (identity_url or "").rstrip("/")
    secret = token or os.getenv(INTERNAL_TOKEN_ENV, "")
    if not base or not secret:
        return False

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{base}/internal/cells",
                json=registration.to_payload(),
                headers=auth_headers(secret),
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


__all__ = [
    "ENV_CELL_ENDPOINT",
    "ENV_CELL_REGION",
    "CellRegistration",
    "register_cell",
    "registration_from_env",
]
