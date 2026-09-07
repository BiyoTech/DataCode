"""Structural guards against re-implementing what the shared layer owns.

A behaviour that must be IDENTICAL across services (how authorization headers
are read, what an error response looks like) has exactly one implementation in
``datacode_common``. A local copy is not a style problem: it is a second answer
to a question the platform already answered, and it drifts silently because
nothing compares the two.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Names whose single definition lives in datacode_common. Re-binding one here
# (``caller_context = _auth.context``) is fine; DEFINING one is not.
SHARED_DEFINITIONS = {
    "CallerContext": "datacode_common.api.caller",
    "LogContextMiddleware": "datacode_common.api.log_context",
    "EnvironmentLockRegistry": "datacode_common.locks.env_locks",
    "_RedisEnvLockBackend": "datacode_common.locks.env_locks",
    # The response shape is a frontend contract. The three per-service copies
    # this replaced had already drifted (one could not carry Retry-After).
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
