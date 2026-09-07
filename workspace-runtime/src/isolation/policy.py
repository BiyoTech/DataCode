"""The runtime's effective isolation policy (design 04 §5, design 06 P6a).

``conf/isolation-profiles.yaml`` says which profiles the DEPLOYMENT wants;
this module says which ones THIS PROCESS can execute, and hands the intersection
to callers. Keeping the two apart matters: a laptop without Kubernetes and a
production cluster read the identical file, and a missing backend must look like
a missing backend in the logs — not like somebody quietly editing the security
posture.

Level availability here:

* ``in_process`` — always. Pure file IO and YAML loading need no vehicle.
* ``process``    — the persistent executor (``executor_enabled``) or the local
  one-shot task backend. Both fork a hardened child.
* ``container``  — the Kubernetes task backend only.

When nothing but ``in_process`` is available, loading fails: L1 assumes its
input cannot execute host code, so it must never become the fallback for work
that can.
"""

from __future__ import annotations

from typing import Optional

from datacode_common.isolation import (
    IsolationLevel,
    IsolationPolicy,
    load_isolation_policy,
)

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Backend identifiers accepted by ``settings.task_backend``.
BACKEND_DISABLED = "disabled"
BACKEND_LOCAL = "local"
BACKEND_KUBERNETES = "kubernetes"

_policy: Optional[IsolationPolicy] = None


def available_levels() -> frozenset[IsolationLevel]:
    """Compute the isolation levels this process can actually execute."""
    levels = {IsolationLevel.IN_PROCESS}

    backend = (settings.task_backend or BACKEND_DISABLED).strip().lower()
    if settings.executor_enabled or backend == BACKEND_LOCAL:
        levels.add(IsolationLevel.PROCESS)
    if backend == BACKEND_KUBERNETES:
        levels.add(IsolationLevel.CONTAINER)

    return frozenset(levels)


def runtime_isolation_policy(*, refresh: bool = False) -> IsolationPolicy:
    """Return the policy restricted to what this process can execute.

    Cached: the answer depends only on configuration, and every gated call site
    asks for it.
    """
    global _policy
    if _policy is not None and not refresh:
        return _policy

    levels = available_levels()
    backend = (settings.task_backend or BACKEND_DISABLED).strip().lower()
    policy = load_isolation_policy(refresh=refresh).restricted_to(
        levels,
        reason=f"task_backend={backend}, executor_enabled={settings.executor_enabled}",
    )

    default = policy.default_profile
    logger.info(
        "isolation_policy_ready",
        backend=backend,
        available_levels=sorted(level.value for level in levels),
        enabled_profiles=[p.name for p in policy.enabled_profiles],
        default_profile=default.name if default else None,
    )
    if default is None:
        # Not fatal: in-process work (file IO, YAML loading, git status) is
        # served correctly. But anything that must be sandboxed will be REFUSED,
        # so say so at startup rather than letting the first plan discover it.
        logger.warning(
            "isolation_sandbox_plane_unavailable",
            backend=backend,
            hint="set task_backend=local (single node) or kubernetes (cluster), "
            "or enable executor_enabled; until then every task requiring a "
            "sandbox is refused",
        )
    _policy = policy
    return policy


__all__ = [
    "BACKEND_DISABLED",
    "BACKEND_KUBERNETES",
    "BACKEND_LOCAL",
    "available_levels",
    "runtime_isolation_policy",
]
