"""The permission CATALOG as the Python services see it.

Mirrors the Go side (``shared/go/rbac``) from the same declaration,
``shared/rbac-catalog.json``: the codes are generated into ``_catalog_data`` and
this module adds the lookup helpers. Nothing here reads a file at runtime — a
deployed wheel contains the package only, so the table has to be code.

Why this lives in the shared layer: the control plane and the runtime both
assert permissions, and every hand-written permission string is a chance to
assert a code that no role can hold. ``require_permission`` in both services
validates against :func:`is_catalog_permission` at import time, so such a
mistake stops the service from starting instead of becoming a permanent 403 for
whoever clicks the button.

The catalog is NOT the role model: which permissions a role grants lives in the
identity database and is tenant-editable. This module only answers "does this
permission point exist at all".

Export policy for the ``PERM_*`` constants: only the codes a Python service
actually asserts are re-exported. The Go side consumes the whole catalog (its
route map names every code); the Python side does not, and exporting the rest
would advertise assertion sites that do not exist. Adding an assertion means
adding the constant here — an explicit, one-line edit the ``ImportError``
immediately asks for. The full table is always available as ``PERMISSIONS``.
"""

from __future__ import annotations

from dataclasses import dataclass

from datacode_common.rbac._catalog_data import (
    PERM_TENANT_WORKSPACE_CREATE,
    PERM_WORKSPACE_DELETE,
    PERM_WORKSPACE_ENV_CREATE,
    PERM_WORKSPACE_ENV_DELETE,
    PERM_WORKSPACE_ENV_PROMOTE,
    PERM_WORKSPACE_ENV_READ,
    PERM_WORKSPACE_GIT_BIND,
    PERM_WORKSPACE_GIT_PUSH,
    PERM_WORKSPACE_MEMBER_WRITE,
    PERM_WORKSPACE_READ,
    PERMISSION_ROWS,
    SCOPE_TENANT,
    SCOPE_WORKSPACE,
    SCOPES,
)


@dataclass(frozen=True, slots=True)
class PermissionDef:
    """One entry of the permission catalog."""

    code: str
    scope: str
    resource: str
    action: str
    description: str


# Declaration order is preserved so the catalog reads the same in both languages.
PERMISSIONS: tuple[PermissionDef, ...] = tuple(
    PermissionDef(*row) for row in PERMISSION_ROWS
)

# Lookup index. The catalog is a compile-time constant, so building it once at
# import time keeps every assertion O(1) instead of scanning ~28 entries.
_BY_CODE: dict[str, PermissionDef] = {perm.code: perm for perm in PERMISSIONS}


def is_catalog_permission(code: str) -> bool:
    """Whether ``code`` is a registered permission point.

    Anything else is unreachable by construction: the identity service refuses
    to compose a role out of an unknown code, so no account can ever hold it.
    """
    return code in _BY_CODE


def scope_of(code: str) -> str | None:
    """The scope ``code`` belongs to, or ``None`` when it is not registered."""
    perm = _BY_CODE.get(code)
    return perm.scope if perm is not None else None


def permissions_in_scope(scope: str) -> tuple[str, ...]:
    """Every registered code of one scope, in declaration order."""
    return tuple(perm.code for perm in PERMISSIONS if perm.scope == scope)


__all__ = [
    # The full table and its query helpers — the drift tests assert against
    # these, and ``is_catalog_permission`` is what ``require_permission``
    # validates every asserted code against.
    "PERMISSIONS",
    "PermissionDef",
    "SCOPES",
    "SCOPE_TENANT",
    "SCOPE_WORKSPACE",
    "is_catalog_permission",
    "permissions_in_scope",
    "scope_of",
    # The codes Python services assert today (see the export policy above):
    # control's workspace + environment routes, and the runtime's
    # promote-to-production gate.
    "PERM_TENANT_WORKSPACE_CREATE",
    "PERM_WORKSPACE_DELETE",
    "PERM_WORKSPACE_ENV_CREATE",
    "PERM_WORKSPACE_ENV_DELETE",
    "PERM_WORKSPACE_ENV_PROMOTE",
    "PERM_WORKSPACE_ENV_READ",
    "PERM_WORKSPACE_GIT_BIND",
    "PERM_WORKSPACE_GIT_PUSH",
    "PERM_WORKSPACE_MEMBER_WRITE",
    "PERM_WORKSPACE_READ",
]
