"""Contracts this service must keep with the shared layer.

Some behaviours are only correct if EVERY service implements them identically —
how the gateway's authorization headers are read, how a request's log context is
bound, how an environment lock is taken. A local copy of one of those is not a
style problem: it is a second, unreviewed answer to a question the platform
already answered, and it drifts silently because nothing compares the two.

The failures these guard against are real ones from this repository: an
environment-lock copy that read a retired variable prefix, so its deployment
override never applied; and a second working-copy validator that skipped both
the layout check and the lease gate, so a request could be served by a replica
that does not hold the copy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Names whose single definition lives in datacode_common. Re-binding one here
# (``Caller = _auth.Caller``) is fine; DEFINING one is not.
SHARED_DEFINITIONS = {
    "CallerContext": "datacode_common.api.caller",
    "LogContextMiddleware": "datacode_common.api.log_context",
    "EnvironmentLockRegistry": "datacode_common.locks.env_locks",
    "_RedisEnvLockBackend": "datacode_common.locks.env_locks",
    # The response shape is a frontend contract. A local re-definition is not
    # just duplication: when the codeloc copy drifted it could not carry a
    # Retry-After header, and merging the drifted copies back under one
    # handler silently dropped the header until a contract test caught it.
    "ServiceError": "datacode_common.errors",
}


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_local_copy_of_a_shared_definition(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = SHARED_DEFINITIONS.get(node.name)
            assert owner is None, (
                f"{path.relative_to(SRC)}:{node.lineno} defines {node.name}, which "
                f"belongs to {owner}. Two implementations of a cross-service "
                "protocol drift, and the drift is invisible until production."
            )


def test_exactly_one_working_copy_validator():
    """A second validator is how a weaker one gets used by accident.

    The Dagster surface used to carry its own: it checked containment and
    existence but not the five-segment layout, and it never consulted the lease.
    Its routes are mounted on the SAME app, so they were reachable on a replica
    that does not hold the working copy.
    """
    definitions = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "validate_workspace_path"
            ):
                definitions.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert len(definitions) == 1, (
        f"expected exactly one working-copy validator, found: {definitions}"
    )
    assert definitions[0].startswith("api/deps.py:"), (
        f"the validator must stay next to the lease gate, found at {definitions[0]}"
    )


def test_every_workspace_path_route_goes_through_the_lease_gate():
    """Behavioural twin of the check above, over the assembled app."""
    from fastapi.routing import APIRoute

    from src.api.deps import validate_workspace_path
    from src.main import app

    def api_routes(router) -> list[APIRoute]:
        """Flatten the router tree, including the wrappers include_router adds."""
        found: list[APIRoute] = []
        for entry in router.routes:
            if isinstance(entry, APIRoute):
                found.append(entry)
            inner = getattr(entry, "original_router", None)
            if inner is not None:
                found.extend(api_routes(inner))
        return found

    checked = 0
    for route in api_routes(app):
        for dependency in route.dependant.dependencies:
            call = dependency.call
            if getattr(call, "__name__", "") != "validate_workspace_path":
                continue
            checked += 1
            assert call is validate_workspace_path, (
                f"{route.path} validates its working copy with a different "
                "implementation than src.api.deps — the lease gate lives there"
            )

    # The assertion above is vacuous if nothing matched.
    assert checked > 0, "no workspace-path route found; the scan is not working"
