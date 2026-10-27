"""This service's handle on the shared environment lock.

The protocol itself — registry, Redis CAS backend, watchdog, degradation — is in
``datacode_common.locks``: control-plane lifecycle operations and workspace-runtime
applies MUST contend for the same lock, and a second copy of a mutual-exclusion
protocol is a second chance to diverge. What stays here is what is genuinely
local: this service's settings, its busy error, and how it derives a lock key
from a working-copy path.

Both deployments must select the Redis backend in any multi-replica topology;
see the shared module for why the TTL is a crash-recovery bound and not an
operation timeout.
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
    "environment_lock_by_key",
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
    env_prefix="CONTROL_SERVICE",
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


@asynccontextmanager
async def environment_lock_by_key(
    workspace_id: str, environment: str, *, operation: str
) -> t.AsyncIterator[None]:
    """Like :func:`environment_lock` but addressed by registry identity.

    Destruction starts from an ``environment_id``, before any working copy is
    involved, so it cannot derive the key from a path.
    """
    async with locks.acquire(
        key=environment_lock_key(workspace_id, environment),
        environment=environment,
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
