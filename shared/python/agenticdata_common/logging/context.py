"""Per-request logging context, shared by every Python service.

Every log line should be attributable without the call site passing identity
around. structlog's context variables carry that identity for the duration of
one request, and the processor chain merges them into each record.

Two binding points cover a service:

* :func:`bind_request_context` — the gateway-issued identity headers, bound by
  HTTP middleware;
* :func:`bind_workspace_context` — the working copy a request operates on,
  bound where the workspace path is validated.

``workspace_path`` is what tells :class:`~agenticdata_common.logging.
workspace_sink.WorkspaceLogSink` where to mirror the line, so binding it is
what makes a record land in the right workspace directory.
"""

from __future__ import annotations

from typing import Optional

import structlog

# Header names the gateway controls and every service reads.
TRACE_ID_HEADER = "X-Trace-ID"
USER_ID_HEADER = "X-User-ID"
TENANT_ID_HEADER = "X-Tenant-ID"


def clear_context() -> None:
    """Drop all bound values. Called at the start of every request."""
    structlog.contextvars.clear_contextvars()


def bind_request_context(
    *,
    trace_id: str = "",
    user_id: str = "",
    tenant_id: str = "",
) -> None:
    """Bind the gateway-issued identity of the current request."""
    _bind(trace_id=trace_id, user_id=user_id, tenant_id=tenant_id)


def bind_workspace_context(
    workspace_path: str,
    *,
    tenant_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    environment: Optional[str] = None,
) -> None:
    """Bind the working copy the current request operates on.

    Segments are passed in rather than re-parsed: the caller has just decoded
    them while validating the path.
    """
    _bind(
        workspace_path=workspace_path,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        environment=environment,
    )


def _bind(**values: Optional[str]) -> None:
    """Bind the non-empty subset, so an absent value never masks a bound one."""
    present = {key: value for key, value in values.items() if value}
    if present:
        structlog.contextvars.bind_contextvars(**present)
