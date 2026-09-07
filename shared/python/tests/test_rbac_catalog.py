"""Permission catalog contract tests.

The catalog is the platform's single declaration of which permission codes exist.
``shared/rbac-catalog.json`` is the source; the Go and Python constant tables are
generated from it, and each language asserts its own table against the JSON here
and in ``shared/go/rbac/catalog_test.go``. A drift therefore fails a test suite
instead of surfacing in production as a permission code that no role can hold —
which is precisely how ``git:credential:write`` shipped as a permanent 403 on
every Git-credential write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacode_common.rbac import (
    PERM_WORKSPACE_ENV_PROMOTE,
    PERM_WORKSPACE_READ,
    PERMISSIONS,
    SCOPE_TENANT,
    SCOPE_WORKSPACE,
    SCOPES,
    is_catalog_permission,
    permissions_in_scope,
    scope_of,
)

# shared/python/tests/ -> shared/
CATALOG_PATH = Path(__file__).resolve().parents[2] / "rbac-catalog.json"


@pytest.fixture(scope="module")
def source() -> dict:
    """The shared declaration both languages are generated from."""
    assert CATALOG_PATH.exists(), f"missing shared catalog: {CATALOG_PATH}"
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert data["permissions"], "the shared catalog is empty; this check is vacuous"
    return data


def test_permissions_match_shared_source(source: dict) -> None:
    """The generated table must equal the declaration, field by field."""
    declared = source["permissions"]
    assert len(PERMISSIONS) == len(declared)
    for got, want in zip(PERMISSIONS, declared, strict=True):
        assert got.code == want["code"]
        assert got.scope == want["scope"]
        assert got.resource == want["resource"]
        assert got.action == want["action"]
        assert got.description == want["description"]


def test_scopes_match_shared_source(source: dict) -> None:
    assert list(SCOPES) == source["scopes"]


def test_permission_codes_are_unique() -> None:
    codes = [perm.code for perm in PERMISSIONS]
    assert len(codes) == len(set(codes))


def test_every_permission_is_reachable_through_a_builtin_role(source: dict) -> None:
    """A permission no shipped role grants is a dead endpoint, not a strict one.

    Role seeding belongs to identity-service, so this is asserted against the
    declaration rather than against a Python-side copy of the templates.
    """
    granted: set[str] = set()
    for role in source["roles"]:
        permissions = role["permissions"]
        if isinstance(permissions, str):
            # Wildcard form: the role gets every code of its own scope.
            assert permissions == "*" + role["scope"]
            granted.update(permissions_in_scope(role["scope"]))
        else:
            granted.update(permissions)

    unreachable = [perm.code for perm in PERMISSIONS if perm.code not in granted]
    assert not unreachable, f"no built-in role grants: {unreachable}"


def test_role_templates_only_reference_registered_codes_of_their_scope(source: dict) -> None:
    for role in source["roles"]:
        permissions = role["permissions"]
        if isinstance(permissions, str):
            continue
        assert permissions, f"role {role['code']} grants nothing"
        for code in permissions:
            assert is_catalog_permission(code), f"{role['code']} references unknown {code}"
            assert scope_of(code) == role["scope"], f"{role['code']} crosses scopes with {code}"


def test_unregistered_codes_are_rejected() -> None:
    """The guard every permission assertion goes through.

    ``git:credential:write`` is kept in this list on purpose: it is the code that
    was asserted by the control plane while being absent from the catalog, making
    the endpoint unreachable for every role including admin.
    """
    for code in ("", "git:credential:write", "workspace:manage", "workspace:read:extra"):
        assert not is_catalog_permission(code)
        assert scope_of(code) is None

    assert is_catalog_permission(PERM_WORKSPACE_READ)
    assert scope_of(PERM_WORKSPACE_READ) == SCOPE_WORKSPACE
    assert scope_of(PERM_WORKSPACE_ENV_PROMOTE) == SCOPE_WORKSPACE


def test_permissions_in_scope_partitions_the_catalog() -> None:
    tenant = permissions_in_scope(SCOPE_TENANT)
    workspace = permissions_in_scope(SCOPE_WORKSPACE)
    assert tenant and workspace
    assert not set(tenant) & set(workspace)
    assert len(tenant) + len(workspace) == len(PERMISSIONS)
    assert permissions_in_scope("nonexistent") == ()


def test_permission_code_encodes_its_scope() -> None:
    """A code is the wire form of its scope, which the frontend groups on."""
    for perm in PERMISSIONS:
        assert perm.code.startswith(perm.scope + ":")
        assert perm.code.endswith(":" + perm.action)
