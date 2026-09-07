"""Permission catalog shared by the Python services.

See ``catalog.py`` for the contract and the export policy; the codes themselves
are generated from ``shared/rbac-catalog.json``, the platform's single
permission declaration.
"""

from datacode_common.rbac.catalog import (
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
    PERMISSIONS,
    SCOPE_TENANT,
    SCOPE_WORKSPACE,
    SCOPES,
    PermissionDef,
    is_catalog_permission,
    permissions_in_scope,
    scope_of,
)

__all__ = [
    "PERMISSIONS",
    "PermissionDef",
    "SCOPES",
    "SCOPE_TENANT",
    "SCOPE_WORKSPACE",
    "is_catalog_permission",
    "permissions_in_scope",
    "scope_of",
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
