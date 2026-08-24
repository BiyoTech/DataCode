"""Tests for the environment-level serial lock (core.env_locks)."""

from __future__ import annotations

import asyncio

import pytest

from src.core import env_locks
from src.core.env_locks import (
    check_environment_lock,
    environment_lock,
    environment_lock_by_key,
    environment_lock_key,
)
from src.utils.errors import EnvironmentBusyError, WorkspacePathInvalidError
from src.workspace.identity import workspace_base_path

_BASE = workspace_base_path()
# Two members holding their own working copy of the SAME environment.
_ALICE = f"{_BASE}/t1/ws-t1-sales/alice/envs/dev_alice"
_BOB = f"{_BASE}/t1/ws-t1-sales/bob/envs/dev_alice"
# Another environment of the same workspace.
_OTHER_ENV = f"{_BASE}/t1/ws-t1-sales/alice/envs/prod"

_KEY = environment_lock_key("ws-t1-sales", "dev_alice")


def test_lock_key_is_workspace_and_environment_scoped():
    assert environment_lock_key("ws-t1-sales", "dev_alice") == "env:ws-t1-sales:dev_alice"


async def test_rejects_concurrent_operation_on_same_environment():
    async with environment_lock(_ALICE, operation="apply"):
        # A different member's working copy of the same environment shares the
        # same SQLMesh state, so it must be refused, not queued.
        with pytest.raises(EnvironmentBusyError) as excinfo:
            async with environment_lock(_BOB, operation="apply"):
                pass

    assert "dev_alice" in excinfo.value.message
    assert excinfo.value.status_code == 409


async def test_lock_by_key_contends_with_path_derived_lock():
    """Destruction locks by registry identity; it must serialize with apply."""
    async with environment_lock(_ALICE, operation="apply"):
        with pytest.raises(EnvironmentBusyError):
            async with environment_lock_by_key(
                "ws-t1-sales", "dev_alice", operation="destroy"
            ):
                pass


async def test_other_environments_are_unaffected():
    async with environment_lock(_ALICE, operation="apply"):
        async with environment_lock(_OTHER_ENV, operation="apply"):
            pass


async def test_lock_is_released_after_failure():
    with pytest.raises(RuntimeError):
        async with environment_lock(_ALICE, operation="apply"):
            raise RuntimeError("apply exploded")

    # Released despite the exception: the next operation may proceed.
    async with environment_lock(_ALICE, operation="apply"):
        pass


async def test_serial_operations_are_allowed():
    for _ in range(3):
        async with environment_lock(_ALICE, operation="apply"):
            await asyncio.sleep(0)


async def test_rejects_path_outside_the_platform_layout():
    with pytest.raises(WorkspacePathInvalidError):
        async with environment_lock("/tmp/not-a-working-copy", operation="apply"):
            pass


def test_check_ignores_paths_outside_the_layout():
    """The advisory pre-check must not turn legacy paths into 400s."""
    check_environment_lock("/tmp/not-a-working-copy", operation="commit")


async def test_check_rejects_while_held_and_passes_after_release():
    async with environment_lock(_ALICE, operation="apply"):
        with pytest.raises(EnvironmentBusyError):
            check_environment_lock(_BOB, operation="apply")

    check_environment_lock(_BOB, operation="apply")


async def test_lock_of_a_dead_owner_is_reclaimed():
    """A lock whose owning task is gone must not block the environment forever.

    Simulates the leak this guards against: the task that holds the lock is
    cancelled at an await point inside the critical section, so its ``finally``
    never releases. The next caller reclaims it instead of getting 409 forever.
    """
    acquired = asyncio.Event()

    async def leak_the_lock():
        holder = await env_locks._registry._holder(_KEY)
        await holder.lock.acquire()
        holder.operation = "apply"
        holder.acquired_at = asyncio.get_running_loop().time()
        holder.owner = asyncio.current_task()
        acquired.set()
        await asyncio.Event().wait()  # never returns; cancelled below

    task = asyncio.create_task(leak_the_lock())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The owner is done, so the lock is reclaimed rather than reported busy.
    async with environment_lock(_BOB, operation="apply"):
        pass


async def test_live_owner_keeps_the_lock_however_long_it_runs():
    """A long-running apply must NEVER be preempted.

    A backfill can legitimately run for hours; taking its lock away would let a
    second apply write the shared SQLMesh state concurrently, which is strictly
    worse than making the second caller wait for a retry.
    """
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def long_apply():
        async with environment_lock(_ALICE, operation="apply"):
            acquired.set()
            await release.wait()

    task = asyncio.create_task(long_apply())
    await acquired.wait()

    # Age the holder arbitrarily: elapsed time must not grant the lock away.
    env_locks._registry._holders[_KEY].acquired_at -= 100_000
    with pytest.raises(EnvironmentBusyError):
        check_environment_lock(_BOB, operation="apply")
    with pytest.raises(EnvironmentBusyError):
        async with environment_lock(_BOB, operation="apply"):
            pass

    release.set()
    await task

    async with environment_lock(_BOB, operation="apply"):
        pass


async def test_idle_holders_are_forgotten():
    """Released holders leave the registry so it cannot grow unbounded."""
    async with environment_lock(_ALICE, operation="apply"):
        assert _KEY in env_locks._registry._holders
    assert _KEY not in env_locks._registry._holders
