"""Dependency endpoints — the platform baseline, made visible.

One read-only surface with two questions behind it:

* ``GET /deps/baseline`` — what does the platform already provide? Until this
  existed the baseline was an implicit fact (whatever some image happened to
  contain), discoverable only by declaring a version and being refused. A user
  cannot write a compatible declaration against a list they cannot see, and an
  agent writing ``pyproject.toml`` had nothing to write it against.
* ``GET /deps/status`` — the same read the asset page performs, addressable on
  its own so the editor can refresh the diagnostic for one file without asking
  for a whole deployment envelope.

Both are pure reads. Installing is deliberately NOT here: it takes minutes and
belongs behind the reload the user explicitly triggers (or ``dc deps sync``),
never behind a GET.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.deps import WorkspacePath
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/v1/deps", tags=["deps"])

logger = get_logger(__name__)

# Names a user is likely to look for when writing an asset, in the order they
# matter. The full baseline runs to several hundred distributions — mostly
# transitive — which is a list nobody reads; these are the ones the scaffold and
# the skill documentation talk about.
#
# Ordering is editorial and the API says so by returning it separately: this is
# "what to write against", while `packages` remains the complete answer.
HIGHLIGHTED = (
    "dagster",
    "dagster-webserver",
    "sqlmesh",
    "getdaft",
    "duckdb",
    "pandas",
    "numpy",
    "pyarrow",
    "scikit-learn",
    "scipy",
    "httpx",
    "pydantic",
    "sqlglot",
)


@router.get("/baseline")
async def get_baseline() -> dict:
    """What a code location can import without declaring anything.

    Returns:
        ``packages``: every distribution as ``{name, version, suggested}``,
        where ``suggested`` is the requirement string that accepts the shipped
        version — so a client can offer "declare this" without reimplementing
        the rule. ``highlighted``: the subset worth showing first.
        ``python_version`` / ``platform_tag``: the interpreter the overlay must
        be built for, which is the other half of a compatible declaration.
    """
    from datacode_common.python_deps import (
        interpreter_platform_tag,
        interpreter_python_version,
    )

    from src.deps.baseline import platform_baseline

    baseline = platform_baseline()
    packages = [
        {"name": name, "version": version, "suggested": f"{name}>={version}"}
        for name, version in sorted(baseline.items())
    ]
    return {
        "packages": packages,
        "highlighted": [
            {"name": name, "version": baseline[name], "suggested": f"{name}>={baseline[name]}"}
            for name in HIGHLIGHTED
            if name in baseline
        ],
        "python_version": interpreter_python_version(),
        "platform_tag": interpreter_platform_tag(),
    }


@router.get("/status")
async def get_status(workspace_path: WorkspacePath) -> dict:
    """State of one working copy's declared dependencies. Installs nothing."""
    from src.deps.service import get_deps_service

    return get_deps_service().inspect(workspace_path).to_dict()


__all__ = ["router"]
