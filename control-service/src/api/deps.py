"""Shared FastAPI dependencies for control-service routes.

Provides the caller's authorization context, bound to this service's 403. The
protocol lives in ``datacode_common.api`` because every service behind the
gateway must read the same headers to the same conclusion — a second, slightly
different reading would be a second authorization decision nobody reviews.

The control plane owns no working copy, so — unlike the runtime — there is no
workspace-path dependency here: endpoints are addressed by workspace_id /
environment_id and the paths are derived where they are used.
"""

from datacode_common.api import CallerAuth, CallerContext

from src.utils.errors import PermissionDeniedError

_auth = CallerAuth(permission_denied=PermissionDeniedError)

caller_context = _auth.context
#: FastAPI dependency exposing the authorization context of the current request.
Caller = _auth.Caller
#: Builds a dependency asserting a permission the gateway must have granted.
#: The code is validated against the shared catalog at import time.
require_permission = _auth.require_permission

__all__ = [
    "Caller",
    "CallerContext",
    "caller_context",
    "require_permission",
]
