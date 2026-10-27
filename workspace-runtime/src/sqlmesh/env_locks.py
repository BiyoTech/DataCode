"""This service's handle on the shared environment lock.

The protocol itself — registry, Redis CAS backend, watchdog, degradation — is in
``datacode_common.locks``: this service's apply path and control-service's
lifecycle operations MUST contend for the same lock, and a second copy of a
mutual-exclusion protocol is a second chance to diverge. It already had: the
copy that used to live here read a RETIRED environment-variable prefix, so the
backend override the deployment injects never applied. The prefix is now passed
in, which makes that class of drift unrepresentable.

What stays here is what is genuinely local: this service's settings, its busy
error, and how it derives a lock key from a working-copy path.

Why this is never in-process in a multi-replica deployment
----------------------------------------------------------
The lease makes this replica the only WRITER of a working copy, but the lock
below is not about writers of a copy: it is about an ENVIRONMENT, and the other
contender is control-service's destroy / reconcile repair — a different process
that holds no copy. ``cross_service=True`` makes the shared protocol refuse an
``in_memory`` backend at startup (see ``EnvironmentLocks.validate``).
"""

from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager

from datacode_common.locks import (
    EnvironmentLockConfig,
    EnvironmentLocks,
    environment_lock_key,
)

from src.utils.errors import EnvironmentBusyError, WorkspacePathInvalidError
from src.workspace.identity import resolve_workspace_identity

__all__ = [
    "check_environment_lock",
    "environment_lock",
    "environment_lock_key",
    "locks",
]


def _config() -> EnvironmentLockConfig:
    # Imported lazily so the module can be imported before settings resolve.
    from src.config import settings

    return EnvironmentLockConfig(
        backend=settings.env_lock_backend,
        ttl_seconds=float(settings.env_lock_ttl_seconds),
    )


locks = EnvironmentLocks(
    env_prefix="WORKSPACE_RUNTIME",
    config=_config,
    busy_error=EnvironmentBusyError,
    cross_service=True,
)


@asynccontextmanager
async def environment_lock(
    workspace_path: str, *, operation: str
) -> t.AsyncIterator[None]:
    """Serialize a state-mutating operation on the path's environment."""
    identity = resolve_workspace_identity(workspace_path)
    async with locks.acquire(
        key=environment_lock_key(identity.workspace_id, identity.environment),
        environment=identity.environment,
        operation=operation,
    ):
        yield


async def check_environment_lock(workspace_path: str, operation: str) -> None:
    """Best-effort pre-check used before spawning background work.

    Raises :class:`EnvironmentBusyError` so a busy environment answers with an
    immediate 409 — including when the holder is another replica, which is why
    this is async: only the backend can answer that question. Paths outside the
    platform layout have no environment to lock and are ignored.
    """
    try:
        identity = resolve_workspace_identity(workspace_path)
    except WorkspacePathInvalidError:
        return
    await locks.assert_free(
        key=environment_lock_key(identity.workspace_id, identity.environment),
        environment=identity.environment,
        operation=operation,
    )
