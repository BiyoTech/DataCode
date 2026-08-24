"""Environment-level serialization of state-mutating operations.

Why a lock beyond the per-context write lock
--------------------------------------------
Every environment of a workspace shares ONE SQLMesh state schema, and every
member holds their OWN worktree of the same branch. The existing
:class:`~src.core.context_pool.ContextEntry` write lock is keyed by
``(workspace_path, gateway)``, so it serializes one user's own operations but
not two members applying the same environment at the same time — precisely the
case the shared state introduces.

This module closes that gap: the lock key is ``env:{workspace_id}:{environment}``,
derived from the working-copy path, so all members and all gateways of one
environment contend for the same lock.

Behaviour on contention
-----------------------
The lock is never awaited. An apply can run for minutes, and a queued HTTP
request that silently waits is indistinguishable from a hang, so a busy
environment answers immediately with
:class:`~src.utils.errors.EnvironmentBusyError` (409) naming the operation that
holds it. SQLMesh's own optimistic state locking remains the second line of
defence for anything reaching the engine from outside this process.

Safety valve: liveness, not time
--------------------------------
A leaked lock (the owning task died without running its ``finally`` — a killed
task, a crashed thread, a lost event loop) would otherwise lock every member of
the environment out until the process restarts. The lock is therefore released
when its OWNER IS GONE, which is decided by asking the owning
:class:`asyncio.Task` whether it is done — never by a timeout.

A wall-clock TTL would be actively harmful here: a legitimate backfill can run
far longer than any TTL worth configuring, and force-releasing it would let a
second apply start while the first is still writing the shared state — turning
a stall into concurrent writers, which is strictly worse. A task that is alive
keeps its lock, however long it takes; a stuck engine call is bounded by the
apply's own cancellation path, not by this module.

A generation counter keeps a disowned holder from releasing its successor's
lock.

Scope: per service process, which matches the current single-replica deployment.
A multi-replica deployment must back the same interface with a distributed lock
(e.g. a state-database advisory lock).
"""

from __future__ import annotations

import asyncio
import time
import typing as t
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from src.utils.errors import EnvironmentBusyError, WorkspacePathInvalidError
from src.utils.logging import get_logger
from src.workspace.identity import resolve_workspace_identity

logger = get_logger(__name__)


@dataclass
class _Holder:
    """Bookkeeping for one environment's lock.

    ``owner`` is the task that acquired the lock; it is what makes a leaked
    lock detectable without guessing at durations. ``generation`` counts forced
    releases so the disowned holder does not release its successor's lock.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operation: str = ""
    acquired_at: float = 0.0
    owner: t.Optional["asyncio.Task[t.Any]"] = None
    generation: int = 0


class EnvironmentLockRegistry:
    """Registry of per-environment locks, created on first use."""

    def __init__(self) -> None:
        self._holders: t.Dict[str, _Holder] = {}
        # Guards the registry dict itself, not the environments.
        self._registry_lock = asyncio.Lock()

    async def _holder(self, key: str) -> _Holder:
        async with self._registry_lock:
            holder = self._holders.get(key)
            if holder is None:
                holder = _Holder()
                self._holders[key] = holder
            return holder

    async def _drop_holder(self, key: str, holder: _Holder) -> None:
        """Forget an idle holder so the registry cannot grow unbounded."""
        async with self._registry_lock:
            if self._holders.get(key) is holder and not holder.lock.locked():
                del self._holders[key]

    def _release_if_abandoned(self, key: str, holder: _Holder) -> bool:
        """Release a lock whose owning task no longer exists.

        The ONLY reason to take a lock away from its holder: the task that
        acquired it is done, so its ``finally`` will never run and the lock
        would stay held forever. A task that is still running keeps the lock
        regardless of how long the operation takes — a backfill may legitimately
        run for hours, and preempting it would allow two concurrent writers on
        the shared SQLMesh state.

        Returns True when the lock was reclaimed. ``generation`` is bumped so
        the disowned holder cannot release a successor's lock.
        """
        owner = holder.owner
        if not holder.lock.locked() or owner is None or not owner.done():
            return False

        held_for = time.monotonic() - holder.acquired_at if holder.acquired_at else 0.0
        holder.operation = ""
        holder.acquired_at = 0.0
        holder.owner = None
        holder.generation += 1
        holder.lock.release()
        logger.warning(
            "environment_lock_reclaimed_from_dead_owner",
            key=key,
            held_for_s=round(held_for, 3),
            owner_cancelled=owner.cancelled(),
        )
        return True

    def _reject_if_busy(
        self, key: str, holder: _Holder, environment: str, operation: str
    ) -> None:
        """Raise :class:`EnvironmentBusyError` when the lock is currently held."""
        if not holder.lock.locked():
            return
        logger.info(
            "environment_lock_busy",
            key=key,
            requested_operation=operation,
            holding_operation=holder.operation,
            held_for_s=round(time.monotonic() - holder.acquired_at, 3),
        )
        raise EnvironmentBusyError(environment, holder.operation)

    def assert_free(self, key: str, *, environment: str, operation: str) -> None:
        """Fail fast (without acquiring) when the lock is held.

        Lets a caller answer 409 synchronously — e.g. before spawning a
        background task whose lock contention would otherwise only surface as
        an SSE failure after an HTTP 200.
        """
        holder = self._holders.get(key)
        if holder is None:
            return
        if self._release_if_abandoned(key, holder):
            return
        self._reject_if_busy(key, holder, environment, operation)

    @asynccontextmanager
    async def acquire(
        self, *, key: str, environment: str, operation: str
    ) -> t.AsyncIterator[None]:
        """Hold an environment's lock for the duration of the block.

        Raises:
            EnvironmentBusyError: the lock is already held.
        """
        holder = await self._holder(key)
        self._release_if_abandoned(key, holder)
        self._reject_if_busy(key, holder, environment, operation)

        generation = holder.generation
        await holder.lock.acquire()
        holder.operation = operation
        holder.acquired_at = time.monotonic()
        holder.owner = asyncio.current_task()
        logger.info("environment_lock_acquired", key=key, operation=operation)
        try:
            yield
        finally:
            duration_ms = int((time.monotonic() - holder.acquired_at) * 1000) if holder.acquired_at else 0
            if holder.generation == generation and holder.lock.locked():
                holder.operation = ""
                holder.acquired_at = 0.0
                holder.owner = None
                holder.lock.release()
                logger.info(
                    "environment_lock_released",
                    key=key,
                    operation=operation,
                    duration_ms=duration_ms,
                )
            else:
                # The lock was reclaimed (dead owner) and may already belong to
                # a successor; releasing it here — or clearing its bookkeeping —
                # would corrupt the successor's ownership.
                logger.warning(
                    "environment_lock_release_skipped_reclaimed",
                    key=key,
                    operation=operation,
                    duration_ms=duration_ms,
                )
            await self._drop_holder(key, holder)


_registry = EnvironmentLockRegistry()


def environment_lock_key(workspace_id: str, environment: str) -> str:
    """Return the lock key shared by every working copy of one environment."""
    return f"env:{workspace_id}:{environment}"


@asynccontextmanager
async def environment_lock(
    workspace_path: str, *, operation: str
) -> t.AsyncIterator[None]:
    """Serialize a state-mutating operation on the path's environment.

    ``operation`` is recorded so a rejected caller learns what is running
    ("apply", "promote", "destroy", ...).
    """
    identity = resolve_workspace_identity(workspace_path)
    async with _registry.acquire(
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
    async with _registry.acquire(
        key=environment_lock_key(workspace_id, environment),
        environment=environment,
        operation=operation,
    ):
        yield


def check_environment_lock(workspace_path: str, operation: str) -> None:
    """Best-effort pre-check used before spawning background work.

    Raises :class:`EnvironmentBusyError` synchronously so a busy environment
    answers with an immediate 409. Advisory only: the spawned task still
    acquires the real lock, which is what actually serializes operations.
    Paths outside the platform layout have no environment to lock and are
    ignored.
    """
    try:
        identity = resolve_workspace_identity(workspace_path)
    except WorkspacePathInvalidError:
        return
    _registry.assert_free(
        environment_lock_key(identity.workspace_id, identity.environment),
        environment=identity.environment,
        operation=operation,
    )
