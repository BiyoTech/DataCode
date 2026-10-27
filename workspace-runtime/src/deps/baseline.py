"""The platform baseline — what a code location can already import.

One module, because three unrelated callers need the same answer and two of
them must not depend on the third: the read-only status path
(:mod:`src.deps.manifest`), the installer (:mod:`src.deps.service`) and the API
surface that shows the baseline to the user.

Why THIS process's interpreter is the baseline
----------------------------------------------
A Dagster code location imports the user's ``defs/`` package on the
workspace-runtime interpreter — locations deliberately share it rather than
getting a per-workspace virtualenv (see ``CodeLocationManager._spawn_grpc``). So
the set of distributions importable without any overlay is exactly the set this
process has, and that is the only definition of "platform-provided" that
predicts what the user's code will see.

The installer runs elsewhere (the task-runner image, or a hardened child of this
process under the local backend), which is why the baseline is DATA that travels
with an install plan rather than something the installer looks up. Under the
local backend the two interpreters coincide, so the difference is invisible in
development and decisive in production — the reason it is stated here at length.

Which pins may yield, and why that is decided here too
------------------------------------------------------
A pinned baseline gives all several hundred distributions a veto over every
declaration, and the cost of being overridden differs by orders of magnitude
between them: a compiled extension breaks the platform at ABI level, a
pure-Python leaf can only change behaviour inside the one working copy that
asked for it. :func:`relaxable_packages` derives the yieldable subset, and it
has to be derived HERE because the test is "does this distribution ship a
``.so``" — a question only the interpreter that HAS the distribution can answer.
The answer travels with the plan for exactly the same reason the baseline does.
"""

from __future__ import annotations

from typing import Optional

from datacode_common.python_deps import (
    DepsDeclaration,
    PlatformConflict,
    compiled_distributions,
    find_platform_conflicts,
    installed_distributions,
    relaxable_baseline,
)

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Computed once per process. A reverse lookup over every installed distribution
# costs tens of milliseconds, and the answer cannot change while the process
# runs: the image is immutable and nothing installs into it. The asset page asks
# for this on every open, so caching is what keeps that path a set of file reads.
_baseline: Optional[dict[str, str]] = None

# Same, and more expensive: deriving it walks every distribution's file list
# looking for a compiled extension. Cached alongside the baseline it is derived
# from, and invalidated with it.
_relaxable: Optional[frozenset[str]] = None


def platform_baseline() -> dict[str, str]:
    """``canonical name -> version`` of what a code location can import."""
    global _baseline
    if _baseline is None:
        _baseline = installed_distributions()
        logger.info("deps_platform_baseline_resolved", packages=len(_baseline))
    return _baseline


def relaxable_packages() -> frozenset[str]:
    """Baseline pins this deployment is willing to yield to a working copy.

    Pure-Python leaves, minus the platform's own machinery, plus or minus what
    an operator configured (``deps.protected_packages`` /
    ``deps.relaxable_packages``). See
    :func:`datacode_common.python_deps.relaxable_baseline` for the ordering and
    the reasoning.
    """
    global _relaxable
    if _relaxable is None:
        baseline = platform_baseline()
        compiled = compiled_distributions(baseline)
        _relaxable = relaxable_baseline(
            baseline,
            compiled=compiled,
            protected_extra=_split(settings.deps_protected_packages),
            relaxable_extra=_split(settings.deps_relaxable_packages),
        )
        logger.info(
            "deps_relaxable_baseline_resolved",
            # Three numbers, because the interesting failure is a deployment
            # where everything looks protected: an image whose metadata cannot
            # be read reports every distribution as compiled, and the only
            # symptom would otherwise be installs that never yield.
            baseline=len(baseline),
            compiled=len(compiled),
            relaxable=len(_relaxable),
        )
    return _relaxable


def reset_platform_baseline() -> None:
    """Drop the cached baseline and its derived sets. For tests only."""
    global _baseline, _relaxable
    _baseline = None
    _relaxable = None


def conflicts_for(declaration: DepsDeclaration) -> tuple[PlatformConflict, ...]:
    """Requirements in *declaration* the baseline cannot satisfy.

    Pure comparison, microseconds, no IO beyond the cached baseline — which is
    what lets the same verdict be reached while the user is still typing in
    ``pyproject.toml`` instead of after a multi-minute reload.
    """
    return find_platform_conflicts(declaration, platform_baseline())


def _split(raw: str) -> tuple[str, ...]:
    """Split a comma-separated configuration value, dropping blanks."""
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


__all__ = [
    "conflicts_for",
    "platform_baseline",
    "relaxable_packages",
    "reset_platform_baseline",
]
