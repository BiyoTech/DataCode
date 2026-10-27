"""Permission contract tests for the workspace runtime.

Two properties are pinned here.

**Credentials are not this service's to write.** The five CRUD endpoints that used
to live in ``api/rest/git.py`` had no permission assertion at all and read the
owner from a query parameter or a request body. They were dead traffic — the
gateway routes every ``/git/credentials*`` request to the control plane — but they
gave a control-plane table a SECOND writer, which the Global/Cell database split
cannot allow. This service reads credentials only through internal repository
calls, so the routes can never come back.

**Permission codes come from the shared catalog.** This service makes exactly one
assertion, and it is conditional on request content (promoting to production is
decided by the target branch in the body), so it uses ``caller.can(...)`` instead
of a route dependency. That makes a hand-written literal even easier to miss,
hence the structural check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from datacode_common.rbac import PERMISSIONS, is_catalog_permission
from fastapi.routing import APIRoute

ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "api"
CATALOG_CODES = {perm.code for perm in PERMISSIONS}


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
# Credential ownership
# ---------------------------------------------------------------------------


def test_runtime_exposes_no_credential_endpoints(routes: list[APIRoute]) -> None:
    offenders = [f"{sorted(r.methods)} {r.path}" for r in routes if "credential" in r.path]
    assert not offenders, (
        "Git credentials are control-plane data with a single writer: " + "; ".join(offenders)
    )


def test_git_routes_never_take_the_owner_from_the_request(routes: list[APIRoute]) -> None:
    """A user_id query parameter is an impersonation surface.

    The deleted credential endpoints had exactly this shape. Git operations that
    legitimately need the owner take it from the caller context or from a body
    field the gateway rewrites, never from a bare query parameter.
    """
    offenders = []
    for route in routes:
        if not route.path.startswith("/api/v1/git"):
            continue
        query = {param.name for param in route.dependant.query_params}
        if "user_id" in query:
            offenders.append(f"{sorted(route.methods)} {route.path}")
    assert not offenders, "user_id must not be a query parameter: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# Permission code provenance
# ---------------------------------------------------------------------------


def test_no_module_defines_its_own_permission_constant() -> None:
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


def test_no_permission_code_is_asserted_as_a_literal() -> None:
    """``caller.can("workspace:...")`` must not appear anywhere."""
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "can":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value in CATALOG_CODES:
                    offenders.append(f"{path.name}: can({arg.value!r})")
    assert not offenders, (
        "import the code from datacode_common.rbac instead: " + "; ".join(offenders)
    )


def test_the_promotion_gate_uses_a_registered_code() -> None:
    """The one assertion this service makes must be reachable through a role."""
    from src.api.rest.git import PERM_WORKSPACE_ENV_PROMOTE

    assert is_catalog_permission(PERM_WORKSPACE_ENV_PROMOTE)


def test_caller_context_carries_permissions_not_roles() -> None:
    """Services authorize on the granted set; a role is tenant-editable.

    The gateway used to publish role headers that nothing consumed. Anything
    branching on a role name would disagree with the role editor as soon as a
    tenant edited the composition.
    """
    from src.api.deps import CallerContext

    fields = set(CallerContext.model_fields)
    assert "permissions" in fields
    assert not {name for name in fields if "role" in name}
