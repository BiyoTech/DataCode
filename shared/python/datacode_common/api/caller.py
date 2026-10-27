"""The caller's authorization context, as decided upstream by the gateway.

Every Python service behind the gateway reads authorization the same way, so it
is defined once. What each service supplies is its own 403 class — the envelope
differs, the protocol does not.

Why the protocol must not be re-implemented per service: the gateway is the
platform's single authorization center. It verifies the token, resolves roles
and publishes the verdict on headers it strips and rewrites on every request. A
service that parsed those headers slightly differently — a missing strip, a
different empty-identity verdict — would be a second, weaker authorization
decision that nobody reviews.
"""

# NOTE: no `from __future__ import annotations` here. The permission dependency
# below annotates its parameter with a value computed at runtime, and FastAPI
# resolves dependencies from the annotation OBJECT; stringified annotations would
# leave it unable to find the dependency at all.
import typing as t

from fastapi import Depends, Header
from pydantic import BaseModel, Field

from datacode_common.logging import get_logger
from datacode_common.rbac import is_catalog_permission

logger = get_logger(__name__)

# Builds a service's 403 from a message and, optionally, the permission code
# that was required. Both existing services expose exactly this signature.
PermissionDeniedFactory = t.Callable[..., Exception]


class CallerContext(BaseModel):
    """Identity and authorization decided upstream by the gateway.

    Services never re-derive permissions and never read identity from a request
    body or query string for authorization purposes: those are client-supplied,
    while these headers are not.
    """

    user_id: str
    tenant_id: str
    workspace_id: str = ""
    permissions: t.FrozenSet[str] = Field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        """Whether the gateway granted a permission for this request."""
        return permission in self.permissions


class CallerAuth:
    """One service's binding of the shared authorization protocol.

    Usage in a service's ``deps.py``::

        _auth = CallerAuth(permission_denied=PermissionDeniedError)
        caller_context = _auth.context
        Caller = _auth.Caller
        require_permission = _auth.require_permission
    """

    def __init__(self, *, permission_denied: PermissionDeniedFactory) -> None:
        self._permission_denied = permission_denied
        self.context = self._build_context_dependency()
        #: FastAPI dependency exposing the current request's authorization.
        self.Caller = t.Annotated[CallerContext, Depends(self.context)]

    def _build_context_dependency(self) -> t.Callable[..., CallerContext]:
        permission_denied = self._permission_denied

        def caller_context(
            x_user_id: str = Header(default="", alias="X-User-ID"),
            x_tenant_id: str = Header(default="", alias="X-Tenant-ID"),
            x_workspace_id: str = Header(default="", alias="X-Workspace-ID"),
            x_granted_permissions: str = Header(
                default="", alias="X-Granted-Permissions"
            ),
        ) -> CallerContext:
            """Build the caller context from the gateway-controlled headers."""
            if not x_user_id or not x_tenant_id:
                # Reaching a service without a gateway-issued identity means the
                # request bypassed authorization entirely.
                logger.warning("caller_context_missing_identity")
                raise permission_denied(
                    "This request carries no verified identity: it did not pass "
                    "through the API gateway"
                )

            permissions = frozenset(
                code.strip()
                for code in x_granted_permissions.split(",")
                if code.strip()
            )
            return CallerContext(
                user_id=x_user_id,
                tenant_id=x_tenant_id,
                workspace_id=x_workspace_id,
                permissions=permissions,
            )

        return caller_context

    def require_permission(self, permission: str) -> t.Callable[..., CallerContext]:
        """Build a dependency asserting that the gateway granted a permission.

        Defence in depth: the gateway already denied the request if the
        permission was missing, so a failure here means the two layers disagree
        and the request must not proceed.

        The permission code is validated against the shared catalog HERE, at
        import time, because a code outside the catalog can never be granted to
        anyone: it is absent from the permissions table, so it has no checkbox in
        the role editor, and identity-service refuses to compose a role out of
        it. Deferring that error to request time turns a typo into a permanent
        403 that looks like a missing grant — exactly how `git:credential:write`
        made every Git credential write impossible for every role, admin
        included.
        """
        if not is_catalog_permission(permission):
            raise ValueError(
                f"{permission!r} is not a registered permission point "
                "(see shared/rbac-catalog.json); no role could ever hold it"
            )
        permission_denied = self._permission_denied
        caller_dependency = self.Caller

        def dependency(caller: caller_dependency) -> CallerContext:  # type: ignore[valid-type]
            if not caller.can(permission):
                logger.warning(
                    "permission_assertion_failed",
                    user_id=caller.user_id,
                    tenant_id=caller.tenant_id,
                    workspace_id=caller.workspace_id,
                    permission=permission,
                )
                raise permission_denied(
                    "Permission denied: your role does not allow this operation",
                    permission=permission,
                )
            return caller

        return dependency
