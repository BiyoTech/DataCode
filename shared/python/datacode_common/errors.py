"""Platform error codes frozen by design doc 08 §7.

Every code defined here crosses at least one service boundary:

* ``LEASE_NOT_HELD``        — workspace-runtime raises it, the gateway re-routes on it
* ``SANDBOX_*``             — task-runner classifies, workspace-runtime relays
* ``EGRESS_DENIED``         — egress-proxy denies, the sandbox surfaces it to the user
* ``QUOTA_EXCEEDED``        — control-service and runtime both enforce it

A code that crosses a boundary must be defined ONCE. Renamed copies drift
silently: the producer keeps emitting the old literal while the consumer waits
for the new one, and the handling on one side is quietly disabled. That exact
failure — nine of thirteen frozen codes never implemented, and the one
lease-routing code implemented on neither side — is why this module exists.

Codes defined here are the CONTRACT. Service-local codes (SQLMesh plan
failures, Git errors, DB-driver classification) stay in each service's own
``utils.errors`` and are deliberately NOT mirrored here.
"""

from enum import StrEnum
from typing import Final, Optional

__all__ = [
    "PlatformErrorCode",
    "CONTRACT_ERROR_CODES",
    "ServiceError",
    "http_status_for",
    "retryable",
]


class PlatformErrorCode(StrEnum):
    """The frozen platform error codes (design 08 §7).

    The enum member name, the wire literal and the doc-comment's HTTP status
    are one unit: a code without its status is unanswerable, and a status
    without its code is unmatchable. :func:`http_status_for` is derived from
    the same table the design froze, so the two cannot disagree.
    """

    # ── Lease & routing (the gateway is the consumer) ──────────────────
    # The request reached a runtime that does not hold this working copy's
    # lease. The gateway must invalidate its cached route and re-resolve;
    # re-load-balancing would just spread the wrong answer.
    LEASE_NOT_HELD = "LEASE_NOT_HELD"                                    # 409
    # No lease capacity in the cell. Retry-After points at the earliest
    # moment a lease might free up, not at a guess.
    LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"                              # 429
    # Two runtimes claim one working copy; the file lock arbitrates.
    LEASE_SPLIT_BRAIN = "LEASE_SPLIT_BRAIN"                              # 409
    # The working copy is being materialized (git clone + worktree); the
    # request is valid but must wait. 202, not 503: nothing failed.
    WORKSPACE_MATERIALIZING = "WORKSPACE_MATERIALIZING"                  # 202
    # The runtime holding the copy is draining; the copy is moving.
    WORKSPACE_RELOCATING = "WORKSPACE_RELOCATING"                        # 503

    # ── Capacity & tenancy ─────────────────────────────────────────────
    # A tenant-level resource quota was reached. 429 when the resource is a
    # rate/ceiling the tenant has exhausted, 409 when it is a count the
    # request would exceed; the raiser picks per contract semantics.
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"                                    # 409/429
    # The cell is under maintenance or tenant migration.
    CELL_MAINTENANCE = "CELL_MAINTENANCE"                                # 503
    # The working copy's disk is full against its quota.
    INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"                        # 507

    # ── Sandbox execution plane ────────────────────────────────────────
    # The sandbox (container or hardened process) could not be started at
    # all: image pull failure, node capacity, fork failure.
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"                          # 503
    # The task was killed by its own deadline (container timeout or
    # RLIMIT_CPU). The work is lost by design — that is what a deadline is.
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"                                  # 504
    # The task was killed by the memory limit (OOMKill / RLIMIT_AS).
    SANDBOX_OOM = "SANDBOX_OOM"                                          # 507

    # ── Egress ─────────────────────────────────────────────────────────
    # The sandbox's outbound connection was refused by the egress policy.
    # ``detail.destination`` says where it was trying to go.
    EGRESS_DENIED = "EGRESS_DENIED"                                      # 403

    # ── Pre-existing codes the contract explicitly retains ─────────────
    # Both predate this module; §7 freezes them as "保留现有契约". They are
    # here for the same reason as the rest: both cross boundaries (control
    # and runtime both raise ENVIRONMENT_BUSY; STATE_SCHEMA_MISMATCH is
    # consumed by the frontend's repair dialog).
    ENVIRONMENT_BUSY = "ENVIRONMENT_BUSY"                                # 409
    STATE_SCHEMA_MISMATCH = "STATE_SCHEMA_MISMATCH"                      # 409


# The frozen code → HTTP status table (design 08 §7, verbatim).
# QUOTA_EXCEEDED is the one deliberately absent: the contract freezes TWO
# statuses (409 for counts, 429 for rates) and the raiser chooses per
# resource, so a single mapping here would be a lie for one of the two.
# STATE_SCHEMA_MISMATCH is 409 with a top-level inline payload
# (gateway/expected/actual/repair_endpoint) — its shape is pinned by its own
# contract tests on both sides, so only the status is pinned here.
_HTTP_STATUS: Final[dict[str, int]] = {
    PlatformErrorCode.LEASE_NOT_HELD: 409,
    PlatformErrorCode.LEASE_UNAVAILABLE: 429,
    PlatformErrorCode.LEASE_SPLIT_BRAIN: 409,
    PlatformErrorCode.WORKSPACE_MATERIALIZING: 202,
    PlatformErrorCode.WORKSPACE_RELOCATING: 503,
    PlatformErrorCode.CELL_MAINTENANCE: 503,
    PlatformErrorCode.INSUFFICIENT_STORAGE: 507,
    PlatformErrorCode.SANDBOX_UNAVAILABLE: 503,
    PlatformErrorCode.SANDBOX_TIMEOUT: 504,
    PlatformErrorCode.SANDBOX_OOM: 507,
    PlatformErrorCode.EGRESS_DENIED: 403,
    PlatformErrorCode.ENVIRONMENT_BUSY: 409,
    PlatformErrorCode.STATE_SCHEMA_MISMATCH: 409,
}

# Every code the contract froze, in contract order. Tests walk this tuple so
# a code added to the enum without being added here fails loudly, and vice
# versa. 14 members: the twelve new codes of §7 plus the two the section
# explicitly freezes as pre-existing.
CONTRACT_ERROR_CODES: Final[tuple[PlatformErrorCode, ...]] = (
    PlatformErrorCode.LEASE_NOT_HELD,
    PlatformErrorCode.LEASE_UNAVAILABLE,
    PlatformErrorCode.LEASE_SPLIT_BRAIN,
    PlatformErrorCode.WORKSPACE_MATERIALIZING,
    PlatformErrorCode.WORKSPACE_RELOCATING,
    PlatformErrorCode.QUOTA_EXCEEDED,
    PlatformErrorCode.SANDBOX_UNAVAILABLE,
    PlatformErrorCode.SANDBOX_TIMEOUT,
    PlatformErrorCode.SANDBOX_OOM,
    PlatformErrorCode.EGRESS_DENIED,
    PlatformErrorCode.CELL_MAINTENANCE,
    PlatformErrorCode.INSUFFICIENT_STORAGE,
    PlatformErrorCode.ENVIRONMENT_BUSY,
    PlatformErrorCode.STATE_SCHEMA_MISMATCH,
)


def http_status_for(code: PlatformErrorCode) -> int:
    """The contract status for ``code``; ``KeyError`` when unmapped.

    QUOTA_EXCEEDED raises on purpose: callers must choose 409 vs 429 from the
    resource's semantics, and a default here would silently pick one for them.
    """
    return _HTTP_STATUS[code]


def retryable(code: PlatformErrorCode) -> bool:
    """Whether the contract expects the caller to retry after this code.

    Drives the gateway's and frontend's retry policy without each of them
    re-deriving it from statuses: 202 (wait for materialization) and 503
    (relocation, cell maintenance, sandbox start) settle on their own, while
    409/404/507 describe states a retry will not change.
    """
    return http_status_for(code) in (202, 503)


class ServiceError(Exception):
    """Base for a service's structured errors: ``{code, message, detail?}``.

    Every Python service answers errors in this shape because the frontend
    reads ``error_code``/``message``/``detail`` uniformly — the shape is a
    contract, so the base lives here rather than being re-implemented per
    service. There were three copies; the codeloc one had already drifted and
    could not even carry a ``Retry-After`` header.

    ``code`` is the raising service's own enum (or a ``PlatformErrorCode``):
    only the cross-boundary codes are frozen (see ``PlatformErrorCode`` above),
    service-local codes stay local by design.

    Fields:
        code: machine-readable error code for routing/UI decisions.
        message: human-readable summary (may be shown to end users).
        detail: structured context — a dict is INLINED into the response body
            (minus a clobbering ``message`` key, preserved as ``detail_message``),
            a string is nested under ``detail``.
        status_code: HTTP status for this error.
        headers: extra response headers (e.g. ``Retry-After`` on quota or busy).
        error_log_id: set by the raising service's error archive when the error
            was persisted; lets the frontend fetch full context. Stays ``None``
            where archiving does not exist.
    """

    def __init__(
        self,
        code,
        message: str,
        detail: Optional[str] = None,
        status_code: int = 500,
        headers: Optional[dict] = None,
    ):
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code
        # Extra response headers (e.g. Retry-After on quota rejections).
        self.headers = headers
        # Populated by the raising service when the error is persisted to its
        # archive; remains None elsewhere.
        self.error_log_id: Optional[str] = None
        super().__init__(message)

    def to_dict(self) -> dict:
        result = {"error_code": self.code.value, "message": self.message}
        if self.detail:
            result["detail"] = self.detail
        return result

    def to_response_dict(self) -> dict:
        """Public response shape with top-level error fields + structured detail.

        Mirrors FastAPI's ``HTTPException`` body so the frontend can read
        ``error_code``/``message``/``detail`` uniformly. When ``detail`` is a
        dict it is inlined as-is; otherwise it is nested under ``detail``.
        """
        out: dict = {
            "error_code": self.code.value,
            "message": self.message,
        }
        if self.error_log_id:
            out["error_log_id"] = self.error_log_id
        if isinstance(self.detail, dict):
            # Inline the structured payload so the frontend does not have to
            # reach into a nested ``detail`` object. Do not let a nested
            # ``message`` key clobber the authoritative top-level message.
            inlined = dict(self.detail)
            nested_message = inlined.pop("message", None)
            out.update(inlined)
            out["message"] = self.message
            if nested_message and nested_message != self.message:
                out["detail_message"] = nested_message
        elif self.detail is not None:
            out["detail"] = self.detail
        return out
