"""Environment-level serialization of state-mutating operations.

Why a lock beyond per-working-copy serialization
------------------------------------------------
Every environment of a workspace shares ONE SQLMesh state schema, and every
member holds their OWN worktree of the same branch. Per-working-copy
serialization is keyed by ``(workspace_path, gateway)``, so it serializes one
user's own operations but not two members applying the same environment at the
same time — precisely the case the shared state introduces.

This module closes that gap: the lock key is ``env:{workspace_id}:{environment}``,
so all members, all gateways and ALL REPLICAS of one environment contend for the
same lock.

**Cross-service mutual exclusion:** control-service's lifecycle operations
and workspace-runtime's apply path hold the SAME key in the SAME Redis namespace
(``sm:env_lock:...``), so a destroy/create/reconcile cannot rip resources out
from under a running apply, and vice versa. That is why this implementation is
shared rather than copied: two copies of a mutual-exclusion protocol are two
chances to diverge, and a divergence here is not a failing test but two writers
on one state schema. The previous copies had already drifted — one of them read
a retired environment-variable prefix, so its deployment override never applied.

Backends
--------
Two implementations share one interface, selected by
``concurrency.env_lock_backend``:

* **in-process** (default for a single replica): an :class:`asyncio.Lock` per
  environment. A leaked lock is reclaimed when its owning task is GONE —
  liveness, not time: a backfill may legitimately run for hours, and
  force-releasing it would let a second apply start while the first is still
  writing the shared state.

* **Redis** (multi-replica): ``SET NX PX`` with a CAS-style compare-and-delete
  release, so a replica that crashed mid-apply cannot corrupt a successor's
  lock. Redis cannot see remote tasks, so liveness-based reclamation is
  impossible across processes; the equivalent safety valve is a **TTL plus a
  watchdog**: the holder renews the TTL every ``ttl/3`` while it lives, and a
  crashed holder stops renewing, so the lock self-releases after at most one
  TTL. The TTL is therefore NOT an operation timeout — it is the crash
  recovery bound.

Behaviour on contention
-----------------------
The lock is never awaited, on either backend. An apply can run for minutes,
and a queued HTTP request that silently waits is indistinguishable from a
hang, so a busy environment answers immediately with the caller-supplied busy
error (409) naming the operation that holds it. SQLMesh's own optimistic state
locking remains the second line of defence for anything reaching the engine
from outside this process.

Degradation
-----------
When the Redis backend cannot reach the server, acquisitions fall back to the
in-process registry for that call. That protects callers within one replica
but NOT across replicas — the situation is logged (rate-limited) as a degraded
mode rather than silently accepted.

Cross-service participants never choose in-process
---------------------------------------------------
"The lease guarantees one writer per working copy, so the lock can be
in-process" is true of WRITES TO ONE COPY and false of this lock: the other
contender is control-service, a different process that holds no copy at all.
A service that declares ``cross_service=True`` therefore refuses an explicit
``in_memory`` configuration at startup (:meth:`EnvironmentLocks.validate`),
and logs a degradation to in-process at ERROR rather than INFO — it means
destroy and apply are no longer mutually exclusive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
import typing as t
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from datacode_common.logging import get_logger

logger = get_logger(__name__)

# A factory for the 409 raised on contention. Injected because each service
# renders its own error envelope; the protocol below does not care which class
# it is, only that it is raised instead of the caller being made to wait.
BusyErrorFactory = t.Callable[[str, str], Exception]


@dataclass(frozen=True)
class EnvironmentLockConfig:
    """The two settings the lock protocol needs, read fresh on first use.

    ``backend`` is one of ``auto`` | ``redis`` | ``in_memory``. ``ttl_seconds``
    only applies to the Redis backend, where it is the crash recovery bound.
    """

    backend: str
    ttl_seconds: float


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

    def __init__(self, busy_error: BusyErrorFactory) -> None:
        self._busy_error = busy_error
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
        """Raise the caller's busy error when the lock is currently held."""
        if not holder.lock.locked():
            return
        logger.info(
            "environment_lock_busy",
            key=key,
            requested_operation=operation,
            holding_operation=holder.operation,
            held_for_s=round(time.monotonic() - holder.acquired_at, 3),
        )
        raise self._busy_error(environment, holder.operation)

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
            Exception: the caller's busy error, when the lock is already held.
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
            held_s = time.monotonic() - holder.acquired_at if holder.acquired_at else 0.0
            duration_ms = int(held_s * 1000)
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


# ---------------------------------------------------------------------------
# Redis backend (multi-replica)
# ---------------------------------------------------------------------------

# Release the lock only if we still own it. Blind DEL would let a slow holder
# (whose lock already expired and was re-acquired elsewhere) delete its
# successor's lock — the distributed twin of the in-process generation counter.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# Renew the TTL only if we still own it, for the same reason.
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# One degraded-mode warning per interval: a Redis outage lasting minutes must
# not produce a warning per acquisition.
_DEGRADE_WARN_INTERVAL = 60.0


class _RedisEnvLockBackend:
    """Distributed environment locks backed by Redis.

    Ownership is a JSON token unique per acquisition (operation id, holder
    identity). Every mutation is a compare-and-swap on that token, so a
    crashed or slow holder can never interfere with a successor.

    The key namespace (``sm:env_lock``) is shared by every service that mutates
    an environment on purpose: control-plane lifecycle operations and runtime
    applies must contend for the SAME lock.
    """

    def __init__(
        self,
        client,
        *,
        ttl_seconds: float,
        registry: EnvironmentLockRegistry,
        busy_error: BusyErrorFactory,
    ) -> None:
        self._client = client
        self._ttl_seconds = float(ttl_seconds)
        # Where an acquisition lands when Redis is unreachable: still serialized
        # within this replica, which is strictly better than not at all.
        self._registry = registry
        self._busy_error = busy_error
        self._last_degrade_warn = 0.0

    @asynccontextmanager
    async def acquire(
        self, *, key: str, environment: str, operation: str
    ) -> t.AsyncIterator[None]:
        """Hold an environment's lock for the duration of the block.

        Raises:
            Exception: the caller's busy error, when the lock is already held
                by any replica.
        """
        full_key = self._key(key)
        token = uuid.uuid4().hex
        payload = json.dumps(
            {
                "token": token,
                "operation": operation,
                "acquired_at": time.time(),
                "holder": f"{socket.gethostname()}/{os.getpid()}",
            }
        )

        try:
            acquired = await self._client.set(
                full_key, payload, nx=True, px=int(self._ttl_seconds * 1000)
            )
        except Exception as exc:  # redis.RedisError or an OS-level failure
            self._warn_degrade("acquire", key, exc)
            async with self._registry.acquire(
                key=key, environment=environment, operation=operation
            ):
                yield
            return

        if not acquired:
            holding = await self._holding_operation(full_key)
            logger.info(
                "environment_lock_busy",
                backend="redis",
                key=key,
                requested_operation=operation,
                holding_operation=holding,
            )
            raise self._busy_error(environment, holding)

        watchdog = asyncio.create_task(
            self._watchdog(full_key, payload, key, operation)
        )
        logger.info(
            "environment_lock_acquired",
            backend="redis",
            key=key,
            operation=operation,
            ttl_s=self._ttl_seconds,
        )
        try:
            yield
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            await self._release(full_key, payload, key, operation)

    async def assert_free(self, *, key: str, environment: str, operation: str) -> None:
        """Raise the caller's busy error when the lock is held.

        Fails open when Redis is unreachable: this is the advisory pre-check,
        and the real serialization happens at acquire time.
        """
        full_key = self._key(key)
        try:
            payload = await self._client.get(full_key)
        except Exception as exc:
            logger.debug(
                "environment_lock_precheck_skipped_backend_error",
                key=key,
                error=str(exc),
            )
            return
        if payload is None:
            return
        holding = self._operation_of(payload)
        logger.info(
            "environment_lock_busy",
            backend="redis",
            key=key,
            requested_operation=operation,
            holding_operation=holding,
        )
        raise self._busy_error(environment, holding)

    # --- internals --------------------------------------------------------

    def _key(self, key: str) -> str:
        from datacode_common.redis import redis_key

        # One namespace for every service that mutates an environment: see the
        # class docstring.
        return redis_key("sm", "env_lock", key)

    async def _holding_operation(self, full_key: str) -> str:
        """Best-effort name of the operation currently holding the lock."""
        try:
            payload = await self._client.get(full_key)
        except Exception:
            return "unknown"
        if payload is None:
            return "unknown"
        return self._operation_of(payload)

    @staticmethod
    def _operation_of(payload: t.Any) -> str:
        try:
            return str(json.loads(payload).get("operation", "unknown"))
        except (TypeError, ValueError):
            return "unknown"

    async def _watchdog(
        self, full_key: str, payload: str, key: str, operation: str
    ) -> None:
        """Renew the TTL while the holder lives.

        Runs every ttl/3 so a single failed renewal (a brief network blip)
        still leaves another full interval before the TTL lapses. Renewal
        failures are logged, not fatal: the next interval retries, and if the
        holder truly died the TTL is exactly the recovery mechanism.
        """
        interval = self._ttl_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._client.eval(
                    _RENEW_SCRIPT, 1, full_key, payload, int(self._ttl_seconds * 1000)
                )
            except Exception as exc:
                logger.warning(
                    "environment_lock_watchdog_renew_failed",
                    key=key,
                    operation=operation,
                    error=str(exc),
                )
                continue
            if not renewed:
                # The lock expired and was re-acquired (or deleted) elsewhere.
                # Stop renewing: this holder no longer owns it.
                logger.warning(
                    "environment_lock_watchdog_lost_ownership",
                    key=key,
                    operation=operation,
                )
                return

    async def _release(
        self, full_key: str, payload: str, key: str, operation: str
    ) -> None:
        """Compare-and-delete the lock; report the outcome for operators."""
        try:
            released = await self._client.eval(_RELEASE_SCRIPT, 1, full_key, payload)
        except Exception as exc:
            # The lock will expire via TTL. Log loudly: between now and then,
            # other replicas see a busy environment.
            logger.warning(
                "environment_lock_release_failed_ttl_will_reclaim",
                key=key,
                operation=operation,
                ttl_s=self._ttl_seconds,
                error=str(exc),
            )
            return
        if released:
            logger.info(
                "environment_lock_released",
                backend="redis",
                key=key,
                operation=operation,
            )
        else:
            # We lost ownership mid-operation (expired + re-acquired). The
            # successor's lock is untouched, which is the point of CAS.
            logger.warning(
                "environment_lock_release_skipped_ownership_lost",
                key=key,
                operation=operation,
            )

    def _warn_degrade(self, op: str, key: str, exc: Exception) -> None:
        """Rate-limited warning that an acquisition degraded to in-process."""
        now = time.monotonic()
        if now - self._last_degrade_warn < _DEGRADE_WARN_INTERVAL:
            return
        self._last_degrade_warn = now
        logger.warning(
            "environment_lock_degraded_to_in_process",
            op=op,
            key=key,
            error=str(exc),
            impact=(
                "across-replica protection is OFF while degraded: only callers "
                "on this replica are serialized"
            ),
        )


def environment_lock_key(workspace_id: str, environment: str) -> str:
    """Return the lock key shared by every working copy of one environment."""
    return f"env:{workspace_id}:{environment}"


class EnvironmentLockConfigError(RuntimeError):
    """The configured backend cannot provide the exclusion this service needs."""


_UNRESOLVED = object()


class EnvironmentLocks:
    """One service's handle on the environment lock protocol.

    Each service creates a single instance and keeps its own path-based
    wrappers; everything below the key is shared, which is what guarantees two
    services contend for the same lock rather than for two look-alikes.
    """

    def __init__(
        self,
        *,
        env_prefix: str,
        config: t.Callable[[], EnvironmentLockConfig],
        busy_error: BusyErrorFactory,
        cross_service: bool = False,
    ) -> None:
        """
        Args:
            env_prefix: the service's environment-variable prefix, e.g.
                ``CONTROL_SERVICE``. The override is read as
                ``{prefix}_ENV_LOCK_BACKEND``. Injected rather than hard-coded
                so a copy of this module cannot keep reading a retired prefix —
                which is exactly how one deployment override silently stopped
                applying.
            config: reads the service's settings, called on first use so an
                import cannot freeze a value the deployment overrides later.
            busy_error: builds the 409 raised on contention, given the
                environment name and the operation holding the lock.
            cross_service: this service contends with ANOTHER service for the
                same keys (control's lifecycle vs the runtime's apply). Such a
                participant may never be configured in-process: see the module
                docstring.
        """
        if not env_prefix or not env_prefix.isupper():
            raise ValueError(
                f"env_prefix must be the service's uppercase env prefix, got {env_prefix!r}"
            )
        self._env_var = f"{env_prefix}_ENV_LOCK_BACKEND"
        self._config = config
        self._busy_error = busy_error
        self._cross_service = cross_service
        self.registry = EnvironmentLockRegistry(busy_error)
        self._backend: object = _UNRESOLVED
        self._backend_init_lock = asyncio.Lock()

    @property
    def backend_env_var(self) -> str:
        """The environment variable that overrides the configured backend."""
        return self._env_var

    @property
    def cross_service(self) -> bool:
        """Whether another service contends for the same keys."""
        return self._cross_service

    def validate(self) -> str:
        """Check the configured backend at startup; return the effective mode.

        Raises:
            EnvironmentLockConfigError: a cross-service participant is
                configured ``in_memory``. Refusing to boot is the only point
                at which this is visible: at runtime the symptom is a destroy
                that tears down an environment while an apply is writing it.
        """
        mode = self._configured_backend()
        if self._cross_service and mode == "in_memory":
            raise EnvironmentLockConfigError(
                f"environment lock backend is 'in_memory' ({self._env_var} or "
                "concurrency.env_lock_backend), but this service contends with "
                "another service for the same environment locks. An in-process "
                "lock cannot exclude a different process: control-service's "
                "destroy/repair and workspace-runtime's apply would run "
                "concurrently on one environment. Use 'redis' (or 'auto')."
            )
        return mode

    # --- backend selection ------------------------------------------------

    def _configured_backend(self) -> str:
        """Backend mode from settings, with an env override for deployments."""
        env_val = os.getenv(self._env_var, "").strip().lower()
        if env_val:
            return env_val
        return self._config().backend.strip().lower()

    async def _resolve_backend(self) -> object:
        """Pick the backend once, on first use.

        "auto" probes Redis once: a probe per acquisition would add a round trip
        to every operation. The verdict is cached, but every Redis operation
        still fails over to the in-process registry individually, so a Redis
        that dies after startup degrades rather than breaks.
        """
        mode = self._configured_backend()
        if mode == "in_memory":
            self._log_in_process(reason="configured_in_memory")
            return None

        if mode not in ("auto", "redis"):
            logger.warning(
                "environment_lock_unknown_backend_mode", mode=mode, fallback="in_memory"
            )
            self._log_in_process(reason="unknown_backend_mode")
            return None

        from datacode_common.redis import get_redis, redis_available

        client = get_redis()
        if client is None:
            # Platform-level redis.enabled=false — a configuration decision, not
            # an outage. In-process is the documented consequence.
            self._log_in_process(reason="platform_redis_disabled")
            return None

        ttl = self._config().ttl_seconds
        if ttl <= 0:
            logger.warning("environment_lock_invalid_ttl", value=ttl, fallback=120)
            ttl = 120

        if mode == "auto" and not await redis_available():
            self._log_in_process(reason="redis_unreachable")
            return None

        logger.info("environment_lock_redis_backend", ttl_s=ttl)
        return _RedisEnvLockBackend(
            client,
            ttl_seconds=ttl,
            registry=self.registry,
            busy_error=self._busy_error,
        )

    async def _get_backend(self) -> object:
        """Return the cached backend (None = in-process registry)."""
        if self._backend is _UNRESOLVED:
            async with self._backend_init_lock:
                if self._backend is _UNRESOLVED:
                    self._backend = await self._resolve_backend()
        return self._backend

    def _log_in_process(self, *, reason: str) -> None:
        """Record that this process will serialize locally only.

        ERROR for a cross-service participant: the other contender is a
        different process, so "in-process" means there is no exclusion with it
        at all. INFO otherwise, where it is a legitimate single-process choice.
        """
        if self._cross_service:
            logger.error(
                "environment_lock_in_process_backend",
                reason=reason,
                impact=(
                    "cross-service exclusion is OFF: lifecycle operations of "
                    "another service can run concurrently with this one"
                ),
            )
        else:
            logger.info("environment_lock_in_process_backend", reason=reason)

    def reset_backend(self) -> None:
        """Force backend re-resolution. Test isolation only."""
        self._backend = _UNRESOLVED

    @contextlib.contextmanager
    def pinned_in_process(self) -> t.Iterator[None]:
        """Use the in-process backend for the duration of the block.

        A test seam: it keeps a suite that asserts the lock's BEHAVIOUR from
        depending on whether the machine running it happens to have Redis.
        """
        previous = self._backend
        self._backend = None  # None = resolved: in-process registry
        try:
            yield
        finally:
            self._backend = previous

    # --- the protocol -----------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self, *, key: str, environment: str, operation: str
    ) -> t.AsyncIterator[None]:
        """Serialize a state-mutating operation on one environment.

        ``operation`` is recorded so a rejected caller learns what is running
        ("apply", "promote", "destroy", ...).
        """
        backend = await self._get_backend()
        target = self.registry if backend is None else backend
        async with target.acquire(
            key=key, environment=environment, operation=operation
        ):
            yield

    async def assert_free(
        self, *, key: str, environment: str, operation: str
    ) -> None:
        """Advisory pre-check: raise the busy error without acquiring.

        The caller still acquires the real lock afterwards; this exists so a
        busy environment can answer 409 immediately instead of failing later
        inside a background task the client has already been told started.
        """
        backend = await self._get_backend()
        if backend is None:
            self.registry.assert_free(key, environment=environment, operation=operation)
            return
        await backend.assert_free(
            key=key, environment=environment, operation=operation
        )
