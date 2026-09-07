"""Distributed run registry: existence in Redis, ownership stays local.

Both long-running domains track in-flight work per logical key — agent-service
per chat session ("session-42"), workspace-runtime per workspace apply
("/path/to/workspace:default"). Two facts about a run must be visible to every
replica:

* **Is one running at all?** A second apply on the same workspace must be
  rejected wherever the request lands, not only on the replica that started
  the first one.
* **Can it be cancelled?** The task handle lives in the process that spawned
  it; a cancel request routed elsewhere must reach that process.

This registry answers both on top of plain Redis:

* Existence is a key per run (``{prefix}:{domain}:run:{key}``) holding the
  owning replica id, written with ``SET NX EX`` so concurrent starts on
  different replicas resolve atomically. The TTL is the crash-recovery bound:
  a replica that dies without unregistering leaves the key to expire, exactly
  like the environment lock.
* Cancellation is a broadcast: the canceller publishes the run key to the
  domain's control channel; every replica hears it, and the one that owns the
  run executes its local cancel callback. A directed message would need a
  routing table (run -> replica) that can be stale; a broadcast with local
  filtering needs none.

Degradation: every operation is best-effort. Redis down means registration is
local-only (double-start protection narrows to one replica) and cancellation
reaches only runs on the canceller's own replica — both are logged, neither
raises. The registry must never make a working single-replica deployment fail.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from datacode_common.redis.client import get_redis, redis_key
from datacode_common.redis.pubsub import PatternPump, replica_id

logger = logging.getLogger(__name__)

# Unregister only our own registration. A plain DEL would let a crashed
# replica that comes back late delete a registration its successor already
# re-created — the same stale-holder hazard the environment lock guards
# against with its CAS release.
_UNREGISTER_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class DistributedRunRegistry:
    """Cross-replica existence tracking + cancel broadcast for one domain."""

    def __init__(
        self,
        *,
        domain: str,
        cancel_local: Callable[[str], bool],
        client: Optional[Redis] = None,
    ) -> None:
        self._domain = domain
        self._cancel_local = cancel_local
        self._client = client
        self._last_degrade_warn = 0.0
        self._pump = PatternPump(
            domain=f"{domain}-ctl",
            pattern=redis_key(domain, "ctl"),
            on_message=self._on_control_message,
            client=client,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start listening for remote cancel requests."""
        await self._pump.start()

    async def stop(self) -> None:
        """Stop listening and release the pub/sub connection."""
        await self._pump.stop()

    # -- registration ------------------------------------------------------

    async def register(self, run_key: str, *, ttl_seconds: int) -> bool:
        """Record that a run is starting. False means one is already running.

        The check-and-set is atomic in Redis, so two replicas starting the
        same run at the same moment cannot both win. On any Redis failure the
        answer is True (degraded: local-only protection) — a refusal here
        would turn a coordination outage into a service outage.
        """
        try:
            created = await self._redis().set(
                self._run_key(run_key),
                replica_id(),
                nx=True,
                ex=ttl_seconds,
            )
        except (RedisError, OSError) as exc:
            self._warn_degraded("register", run_key, exc)
            return True
        if not created:
            logger.info("run_already_registered domain=%s run_key=%s", self._domain, run_key)
            return False
        return True

    async def refresh(self, run_key: str, *, ttl_seconds: int) -> None:
        """Extend a run's TTL (long-running work keeps its registration)."""
        try:
            await self._redis().expire(self._run_key(run_key), ttl_seconds)
        except (RedisError, OSError) as exc:
            self._warn_degraded("refresh", run_key, exc)

    async def unregister(self, run_key: str) -> None:
        """Remove this replica's registration (run finished or cancelled)."""
        try:
            await self._redis().eval(
                _UNREGISTER_SCRIPT, 1, self._run_key(run_key), replica_id()
            )
        except (RedisError, OSError) as exc:
            self._warn_degraded("unregister", run_key, exc)

    async def exists(self, run_key: str) -> bool:
        """Report whether a run is registered on any replica."""
        try:
            return bool(await self._redis().exists(self._run_key(run_key)))
        except (RedisError, OSError) as exc:
            self._warn_degraded("exists", run_key, exc)
            return False

    # -- cancellation ------------------------------------------------------

    async def cancel(self, run_key: str) -> bool:
        """Cancel a run wherever it lives.

        Order matters: try the local cancel first — it is synchronous with the
        task handle and needs no network. Only when this replica does not own
        the run (or the local task already finished) broadcast the request.
        """
        if self._cancel_local(run_key):
            return True
        return await self._broadcast_cancel(run_key)

    async def _broadcast_cancel(self, run_key: str) -> bool:
        """Ask whichever replica owns the run to cancel it.

        Returns True when the request was published, NOT when the run was
        cancelled — Pub/Sub is fire-and-forget. The caller reports "cancel
        requested"; final confirmation arrives through the normal event stream.
        """
        try:
            receivers = await self._redis().publish(
                redis_key(self._domain, "ctl"),
                json.dumps({"action": "cancel", "run_key": run_key}),
            )
        except (RedisError, OSError) as exc:
            self._warn_degraded("cancel_broadcast", run_key, exc)
            return False
        if receivers == 0:
            # Nobody is listening: either every replica's pump is down or the
            # run already ended. Both present as "nothing to cancel".
            return False
        logger.info(
            "run_cancel_broadcast domain=%s run_key=%s replicas_heard=%s",
            self._domain,
            run_key,
            receivers,
        )
        return True

    async def _on_control_message(self, channel: str, payload: str) -> None:
        """Handle a remote cancel request; ignore anything malformed."""
        try:
            message = json.loads(payload)
        except ValueError:
            logger.warning("run_ctl_malformed_message domain=%s", self._domain)
            return
        if message.get("action") != "cancel":
            return
        run_key = message.get("run_key")
        if not isinstance(run_key, str):
            return
        # Broadcast reaches every replica; only the owner acts. A replica that
        # does not own the run simply ignores it.
        if self._cancel_local(run_key):
            logger.info(
                "run_cancelled_by_remote_request domain=%s run_key=%s",
                self._domain,
                run_key,
            )

    # -- internals ---------------------------------------------------------

    def _redis(self) -> Redis:
        if self._client is not None:
            return self._client
        client = get_redis()
        if client is None:
            # Callers guard with try/except around Redis calls, but a missing
            # client must still raise into those handlers for degradation to
            # apply uniformly.
            raise RedisError("platform redis is disabled")
        return client

    def _run_key(self, run_key: str) -> str:
        return redis_key(self._domain, "run", run_key)

    def _warn_degraded(self, op: str, run_key: str, exc: Exception) -> None:
        import time as _time

        now = _time.monotonic()
        if now - self._last_degrade_warn < 60.0:
            return
        self._last_degrade_warn = now
        logger.warning(
            "run_registry_degraded domain=%s op=%s run_key=%s error=%s",
            self._domain,
            op,
            run_key,
            exc,
        )
