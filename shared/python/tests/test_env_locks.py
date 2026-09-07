"""Tests for the shared environment lock protocol.

Two services contend for the SAME key through this module, so the properties
worth pinning are the ones a divergence would break: who is refused, when a
leaked lock may be taken away, and which environment variable selects the
backend.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from datacode_common.locks import (
    EnvironmentLockConfig,
    EnvironmentLocks,
    environment_lock_key,
)


class Busy(Exception):
    """Stand-in for a service's 409, built the way the services build theirs."""

    def __init__(self, environment: str, operation: str) -> None:
        super().__init__(f"{environment} busy: {operation}")
        self.environment = environment
        self.operation = operation


def _locks(prefix: str = "TEST_SERVICE", backend: str = "in_memory") -> EnvironmentLocks:
    return EnvironmentLocks(
        env_prefix=prefix,
        config=lambda: EnvironmentLockConfig(backend=backend, ttl_seconds=120.0),
        busy_error=Busy,
    )


KEY = environment_lock_key("ws-t1-sales", "dev_alice")


def test_the_key_is_workspace_and_environment_scoped():
    """Not user-scoped: members share one state schema per environment."""
    assert environment_lock_key("ws-t1-sales", "dev_alice") == "env:ws-t1-sales:dev_alice"
    assert environment_lock_key("ws-t1-sales", "prod") != KEY


class TestBackendSelection:
    """The override variable is derived from the caller's prefix.

    The regression: one copy of this protocol kept reading a retired service
    prefix, so the backend override its deployment injected never applied and
    the process silently stayed on the configured default. With the prefix
    injected, a wrong name cannot be written in only one of the copies.
    """

    def test_the_variable_name_follows_the_service_prefix(self):
        assert _locks("CONTROL_SERVICE").backend_env_var == (
            "CONTROL_SERVICE_ENV_LOCK_BACKEND"
        )
        assert _locks("WORKSPACE_RUNTIME").backend_env_var == (
            "WORKSPACE_RUNTIME_ENV_LOCK_BACKEND"
        )

    def test_a_lowercase_or_empty_prefix_is_refused(self):
        # Env vars are uppercase; accepting anything else would produce a
        # variable no deployment sets, i.e. the defect this guards against.
        for bad in ("", "control_service", "Control"):
            with pytest.raises(ValueError):
                _locks(bad)

    async def test_the_environment_overrides_the_configured_backend(self, monkeypatch):
        locks = _locks("TEST_SERVICE", backend="redis")
        monkeypatch.setenv("TEST_SERVICE_ENV_LOCK_BACKEND", "in_memory")
        # in_memory resolves to None (the in-process registry) without ever
        # touching Redis, which is how we observe the override took effect.
        assert await locks._get_backend() is None

    async def test_an_unknown_mode_degrades_instead_of_failing(self, monkeypatch):
        locks = _locks("TEST_SERVICE")
        monkeypatch.setenv("TEST_SERVICE_ENV_LOCK_BACKEND", "postgres")
        assert await locks._get_backend() is None


class TestContention:
    async def test_a_second_operation_is_refused_not_queued(self):
        """An apply runs for minutes; a silent wait is indistinguishable from a hang."""
        locks = _locks()
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            with pytest.raises(Busy) as excinfo:
                async with locks.acquire(
                    key=KEY, environment="dev_alice", operation="promote"
                ):
                    pass
        # The refusal names the operation holding the lock, not the one refused.
        assert excinfo.value.operation == "apply"

    async def test_other_environments_are_unaffected(self):
        locks = _locks()
        other = environment_lock_key("ws-t1-sales", "prod")
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            async with locks.acquire(key=other, environment="prod", operation="apply"):
                pass

    async def test_the_lock_is_released_even_when_the_body_raises(self):
        locks = _locks()
        with pytest.raises(RuntimeError):
            async with locks.acquire(
                key=KEY, environment="dev_alice", operation="apply"
            ):
                raise RuntimeError("apply blew up")
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            pass

    async def test_assert_free_answers_without_acquiring(self):
        locks = _locks()
        await locks.assert_free(key=KEY, environment="dev_alice", operation="apply")
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            with pytest.raises(Busy):
                await locks.assert_free(
                    key=KEY, environment="dev_alice", operation="apply"
                )
        # And the pre-check must not have taken the lock itself.
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            pass


class TestLeakedLocks:
    """Reclamation is keyed on LIVENESS, never on elapsed time.

    A backfill may legitimately run for hours; force-releasing on a timer would
    start a second writer on the same state schema.
    """

    async def test_a_lock_held_by_a_dead_task_is_reclaimed(self):
        locks = _locks()
        started = asyncio.Event()

        async def leak() -> None:
            # Enter the block and die inside it, so the finally never runs.
            cm = locks.acquire(key=KEY, environment="dev_alice", operation="apply")
            await cm.__aenter__()
            started.set()
            raise asyncio.CancelledError()

        task = asyncio.create_task(leak())
        await started.wait()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            pass

    async def test_a_long_running_live_task_keeps_its_lock(self):
        locks = _locks()
        holding = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with locks.acquire(
                key=KEY, environment="dev_alice", operation="backfill"
            ):
                holding.set()
                await release.wait()

        task = asyncio.create_task(hold())
        await holding.wait()
        # Age the holder far past any plausible timeout: still refused, because
        # its owner is alive.
        locks.registry._holders[KEY].acquired_at -= 100_000
        with pytest.raises(Busy):
            async with locks.acquire(
                key=KEY, environment="dev_alice", operation="apply"
            ):
                pass
        release.set()
        await task

    async def test_idle_holders_are_forgotten(self):
        """Otherwise the registry grows once per environment ever touched."""
        locks = _locks()
        async with locks.acquire(key=KEY, environment="dev_alice", operation="apply"):
            assert KEY in locks.registry._holders
        assert KEY not in locks.registry._holders
