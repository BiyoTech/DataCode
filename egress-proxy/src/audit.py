"""Egress audit trail (design 04 §3.4).

Every decision is recorded: who asked, when, for what destination, whether it was
allowed and how many bytes crossed. Recording only DENIALS would be the more
obvious choice and the wrong one — exfiltration happens through an *allowed*
destination, so the byte counts on successful tunnels are the interesting signal.

Emitted as structured JSON on stdout. The observability stack that would consume
it is deferred (see the roadmap's Deferred list), so the field names are fixed
here and now, in the shape a pipeline will want later: one flat object per event,
with the tenant and task on every line.

A record NEVER contains the task token, a warehouse credential, or a request body.
The proxy tunnels TLS and cannot see the body even if it wanted to; the token is
identity and is reduced to its claims before logging.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Optional

from datacode_common.egress import EgressDecision
from datacode_common.security.task_token import TaskTokenClaims

logger = logging.getLogger("egress.audit")

# Event names. Stable: they are what a query filters on.
EVENT_CONNECT_DENIED = "egress_denied"
EVENT_CONNECT_ALLOWED = "egress_allowed"
EVENT_TUNNEL_CLOSED = "egress_tunnel_closed"
EVENT_AUTH_FAILED = "egress_auth_failed"


def _emit(event: str, fields: dict[str, Any]) -> None:
    """Write one audit line.

    Straight to stdout rather than through the logging hierarchy: the audit trail
    must not be silenced by a log-level change made for a different reason.
    """
    record = {
        "event": event,
        # UTC, always: the platform stores and reasons in UTC and the display
        # timezone is the frontend's concern.
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    sys.stdout.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def auth_failed(reason: str, *, peer: str = "", target: str = "") -> None:
    """A client could not be identified. No claims exist yet, hence no scope."""
    _emit(EVENT_AUTH_FAILED, {"reason": reason, "peer": peer, "target": target})


def decision(
    claims: TaskTokenClaims,
    outcome: EgressDecision,
    *,
    peer: str = "",
    bytes_used: int = 0,
    byte_quota: int = 0,
) -> None:
    """Record an allow/deny decision for an identified task."""
    _emit(
        EVENT_CONNECT_ALLOWED if outcome.allowed else EVENT_CONNECT_DENIED,
        {
            **claims.audit_fields(),
            **outcome.audit_fields(),
            "peer": peer,
            "detail": outcome.detail,
            "bytes_used": bytes_used,
            "byte_quota": byte_quota,
        },
    )


def tunnel_closed(
    claims: TaskTokenClaims,
    *,
    host: str,
    port: int,
    sent: int,
    received: int,
    duration_ms: int,
    total_used: int,
    error: Optional[str] = None,
) -> None:
    """Record a finished tunnel.

    ``sent`` / ``received`` are from the SANDBOX's point of view, which is the
    direction that matters when reading these records for exfiltration: a large
    ``received`` is a download, a large ``sent`` is data leaving the platform.
    """
    _emit(
        EVENT_TUNNEL_CLOSED,
        {
            **claims.audit_fields(),
            "egress_host": host,
            "egress_port": port,
            "bytes_sent": sent,
            "bytes_received": received,
            "bytes_total": sent + received,
            "bytes_used_cumulative": total_used,
            "duration_ms": duration_ms,
            "error": error or "",
        },
    )


__all__ = [
    "EVENT_AUTH_FAILED",
    "EVENT_CONNECT_ALLOWED",
    "EVENT_CONNECT_DENIED",
    "EVENT_TUNNEL_CLOSED",
    "auth_failed",
    "decision",
    "tunnel_closed",
]
