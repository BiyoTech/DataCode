"""Permission contract tests for the control plane.

Guards the two properties whose absence let an unreachable permission ship:

1. a permission code asserted by a route must exist in the shared catalog, and
   that must be detected when the module is IMPORTED rather than when a user
   clicks the button;
2. permission codes must come from the shared catalog module, not from a string
   literal typed into the route file — a literal is how a code that exists
   nowhere else gets introduced in the first place.

The failure being prevented: `git:credential:write` was asserted here while being
absent from the catalog, so it was absent from the permissions table, had no
checkbox in the role editor and was rejected by identity-service if requested
through the API. Every Git credential write answered 403, for every account
including admin, with no way to grant it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from datacode_common.rbac import PERM_WORKSPACE_READ, is_catalog_permission
from fastapi.routing import APIRoute

from src.api.deps import require_permission

ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "api" / "rest"


def _api_routes(router) -> list[APIRoute]:
    """Flatten a FastAPI router, including the wrappers include_router adds."""
    found: list[APIRoute] = []
    for route in router.routes:
        if isinstance(route, APIRoute):
            found.append(route)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_api_routes(inner))
    return found


@pytest.fixture(scope="module")
def routes() -> list[APIRoute]:
    from src.main import app

    collected = _api_routes(app.router)
    assert collected, "no routes collected; this contract check would be vacuous"
    return collected


# ---------------------------------------------------------------------------
# 1. Unregistered codes cannot be asserted at all
# ---------------------------------------------------------------------------


def test_require_permission_rejects_unregistered_code_at_construction() -> None:
    """Fail fast, at import time, not on the first request that needs it."""
    with pytest.raises(ValueError, match="not a registered permission point"):
        require_permission("git:credential:write")


def test_require_permission_accepts_catalog_code() -> None:
    assert require_permission(PERM_WORKSPACE_READ) is not None


# ---------------------------------------------------------------------------
# 2. No hand-written permission literals
# ---------------------------------------------------------------------------


def _permission_literals(tree: ast.AST) -> list[str]:
    """String literals passed to require_permission(...) anywhere in a module."""
    literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "require_permission":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literals.append(arg.value)
    return literals


def test_no_route_asserts_a_literal_permission_code() -> None:
    """Structural guard: the catalog must be the only source of codes.

    A literal would pass the runtime check as long as it happens to match a
    catalog entry today, and silently become unreachable the moment the code is
    renamed there — which is exactly the drift a single source is meant to end.
    """
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for literal in _permission_literals(tree):
            offenders.append(f"{path.name}: require_permission({literal!r})")
    assert not offenders, (
        "permission codes must be imported from datacode_common.rbac: " + "; ".join(offenders)
    )


def test_no_route_module_defines_its_own_permission_constant() -> None:
    """A local ``PERM_* = "..."`` is the same drift with an extra hop."""
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("PERM_"):
                    offenders.append(f"{path.name}: {target.id} = {node.value.value!r}")
    assert not offenders, (
        "permission constants belong to the shared catalog: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 3. Git credentials stay user-scoped
# ---------------------------------------------------------------------------


def test_git_credential_routes_require_no_permission(routes: list[APIRoute]) -> None:
    """A personal access token is guarded by identity and ownership.

    Asserting a permission here is what broke the endpoint. The gateway route map
    classifies this group as authenticated-only; if a permission is ever wanted,
    both layers and the catalog have to change together.
    """
    credential_routes = [r for r in routes if "/git/credentials" in r.path]
    assert len(credential_routes) == 5, (
        f"expected the 5 credential endpoints, found {[r.path for r in credential_routes]}"
    )

    for route in credential_routes:
        for dependency in route.dependant.dependencies:
            call = dependency.call
            closure = getattr(call, "__closure__", None) or ()
            for cell in closure:
                value = cell.cell_contents
                if isinstance(value, str) and is_catalog_permission(value):
                    pytest.fail(f"{route.path} asserts permission {value!r}")


def test_git_credential_routes_resolve_the_owner_from_the_caller(routes: list[APIRoute]) -> None:
    """Ownership is the boundary, so every endpoint must take the caller.

    Without it a handler would have to read the owner from the request, which is
    precisely the shape that let the runtime's deleted endpoints accept
    ``?user_id=<someone else>``.
    """
    for route in routes:
        if "/git/credentials" not in route.path:
            continue
        params = set(route.dependant.path_params) | {
            param.name for param in route.dependant.query_params
        }
        assert "user_id" not in params, (
            f"{route.path} takes user_id from the request instead of the verified identity"
        )
        names = [dep.name for dep in route.dependant.dependencies]
        assert "caller" in names, f"{route.path} does not depend on the caller context"
