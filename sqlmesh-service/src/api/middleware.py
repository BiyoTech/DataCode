"""HTTP middleware.

Binds the per-request logging context so every record emitted while handling
a request carries the caller's identity and the gateway's trace id, without
any handler having to pass them along.

The workspace itself is bound later, by ``validate_workspace_path``: it is the
only place that knows the request targets a working copy, and it is reached
from both query parameters and request bodies.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from datacode_common.logging.context import (
    TENANT_ID_HEADER,
    TRACE_ID_HEADER,
    USER_ID_HEADER,
    bind_request_context,
    clear_context,
)


class LogContextMiddleware(BaseHTTPMiddleware):
    """Reset and populate the structlog context variables per request."""

    async def dispatch(self, request: Request, call_next):
        # Context variables outlive a request when a worker task is reused, so
        # the previous request's identity must be dropped before binding.
        clear_context()
        bind_request_context(
            trace_id=request.headers.get(TRACE_ID_HEADER, ""),
            user_id=request.headers.get(USER_ID_HEADER, ""),
            tenant_id=request.headers.get(TENANT_ID_HEADER, ""),
        )
        try:
            return await call_next(request)
        finally:
            clear_context()
