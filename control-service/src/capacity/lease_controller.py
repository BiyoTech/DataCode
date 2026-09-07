"""LeaseController — capacity orchestration from the heartbeat stream (P8).

The P4a note deferred three things to this module, and the deferred part of
P8's observability stack fixed its shape: it must decide from the lease
heartbeats (Redis ``rt:{runtime_id}``), NOT from a metrics stack — the roadmap
says so explicitly, because the metrics are deferred but capacity must not be.

What one tick does, in order
----------------------------
1. **Account**: per-tenant lease counts from the heartbeats are written into
   ``tenant_usage`` (concurrent + peak watermark) — the flows that hold leases
   live on the runtime, so this loop is the writer the P5 schema was waiting
   for.
2. **Publish**: the lease-relevant quota slice is re-published to Redis with a
   fresh TTL (see ``src.quota.publisher``).
3. **Decide**: cell-wide utilization = held leases / capacity, with hysteresis
   thresholds (scale up above ``scale_up_threshold``, consider scaling down
   below ``scale_down_threshold``), bounded by min/max replicas.
4. **Act**: scale-up applies immediately (an empty instance harms nothing).
   Scale-down is DRAIN-FIRST: the emptiest non-draining instance is asked to
   drain via its internal endpoint, and the replica count shrinks only once
   its heartbeat disappears (or the drain times out — see below).

Honest limits, stated rather than hidden
----------------------------------------
* **Which pod dies is Kubernetes' choice.** Scaling a StatefulSet down removes
  the highest ordinal, which may not be the drained victim. That is still
  safe — the runtime drains on SIGTERM too (the shutdown hook), so the killed
  pod protects its WIP the same way — it is merely less optimal than draining
  the exact victim. A pod-deletion-based orchestrator would pick the victim
  precisely; that refinement is left for a real-cluster iteration.
* **A drain that never finishes must not wedge scale-down forever.** After
  ``drain_timeout_seconds`` the controller shrinks the count anyway and logs
  loudly; the SIGTERM drain is the backstop for that pod's leases.
* **This controller is cell-local by design** (P7): the heartbeat keys carry
  the cell prefix, so two cells' controllers never see each other's instances.
"""

from __future__ import annotations

import abc
import asyncio
import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from datacode_common.lease import (
    RuntimeHeartbeat,
    decode_runtime_heartbeat,
    runtime_index_redis_key,
    runtime_redis_key,
)

from src.config import settings
from src.quota.publisher import publish_quotas
from src.utils.logging import get_logger

logger = get_logger(__name__)

# How long the controller waits for the drain endpoint's answer. The endpoint
# returns after publishing the draining heartbeat (sub-second); anything
# longer is an unreachable instance, not a slow repository.
DRAIN_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CapacityOutcome:
    """One tick's decision, for logs, tests and the manual-trigger endpoint."""

    utilization: float
    current_replicas: int
    desired_replicas: int
    # scale_up | scale_down | drain_requested | drain_wait | hold
    action: str
    reason: str
    live_instances: int
    held_leases: int


class ReplicaDriver(abc.ABC):
    """How replica counts are read and changed.

    An interface, not a Kubernetes client, so the DECISION LOGIC (thresholds,
    hysteresis, drain-first) is testable without a cluster — the same
    collaborator-injection discipline the reaper follows.
    """

    @abc.abstractmethod
    async def current_replicas(self) -> int:
        """The replica count the orchestrator sees right now."""

    @abc.abstractmethod
    async def set_replicas(self, count: int) -> None:
        """Apply a new replica count."""


class LoggingReplicaDriver(ReplicaDriver):
    """Dev / no-cluster default: tracks a number, changes nothing real.

    Serves two purposes: dev environments get the same decision LOGS as
    production, and the manual-trigger endpoint can demonstrate the policy
    without a cluster.
    """

    def __init__(self, replicas: int = 1) -> None:
        self._replicas = replicas

    async def current_replicas(self) -> int:
        return self._replicas

    async def set_replicas(self, count: int) -> None:
        logger.info("replica_driver_logging_set", replicas=count)
        self._replicas = count


class KubernetesReplicaDriver(ReplicaDriver):
    """Scales the workspace-runtime StatefulSet via the scale subresource.

    The ``kubernetes`` package is imported lazily: control-service stays light
    by default (no cluster SDK in dev), and a deployment that selects this
    driver installs the ``k8s`` extra. The sync client calls run in a worker
    thread so the control loop never blocks the event loop on them.
    """

    def __init__(self, *, namespace: str, statefulset: str) -> None:
        self._namespace = namespace
        self._statefulset = statefulset
        self._api = None

    def _apps_api(self):
        if self._api is None:
            from kubernetes import client, config  # lazy: optional extra

            try:
                config.load_incluster_config()
            except Exception:  # noqa: BLE001 — dev fallback
                config.load_kube_config()
            self._api = client.AppsV1Api()
        return self._api

    async def current_replicas(self) -> int:
        def _read() -> int:
            scale = self._apps_api().read_namespaced_stateful_set_scale(
                self._statefulset, self._namespace
            )
            return int(scale.spec.replicas or 0)

        return await asyncio.to_thread(_read)

    async def set_replicas(self, count: int) -> None:
        def _write() -> None:
            from kubernetes.client import V1Scale

            body = V1Scale(spec={"replicas": count})
            self._apps_api().patch_namespaced_stateful_set_scale(
                self._statefulset, self._namespace, body
            )

        await asyncio.to_thread(_write)
        logger.info(
            "replica_driver_k8s_scaled",
            namespace=self._namespace,
            statefulset=self._statefulset,
            replicas=count,
        )


class LeaseController:
    """Reads heartbeats, keeps usage honest, drives the replica count."""

    def __init__(
        self,
        *,
        client,
        driver: ReplicaDriver,
        usage_reporter: t.Optional[t.Callable[[dict[str, int]], t.Awaitable[None]]] = None,
        quota_publisher: t.Optional[t.Callable[[t.Any], t.Awaitable[int]]] = None,
        drain_requester: t.Optional[
            t.Callable[[str], t.Awaitable[bool]]
        ] = None,
    ) -> None:
        self._client = client
        self._driver = driver
        # Collaborators injectable for tests; the production wiring passes the
        # quota service / publisher / an httpx drain call.
        self._usage_reporter = usage_reporter
        self._quota_publisher = quota_publisher
        self._drain_requester = drain_requester or self._request_drain
        # runtime_id -> when its drain was requested (UTC). In-memory on
        # purpose: a controller restart simply re-requests a drain, which the
        # runtime answers idempotently.
        self._drain_requested_at: dict[str, datetime] = {}

    async def run_once(self) -> CapacityOutcome:
        """One control tick: account, publish, decide, act."""
        heartbeats = await self._read_heartbeats()
        await self._account_usage(heartbeats)
        await self._publish_quotas()

        held = sum(hb.lease_count for hb in heartbeats)
        capacity = sum(hb.max_leases for hb in heartbeats if hb.max_leases > 0)
        utilization = (held / capacity) if capacity > 0 else 0.0

        current = await self._driver.current_replicas()
        outcome = await self._decide(
            heartbeats=heartbeats,
            utilization=utilization,
            current=current,
            held=held,
        )
        logger.info(
            "lease_controller_tick",
            utilization=round(utilization, 3),
            live_instances=len(heartbeats),
            held_leases=held,
            current_replicas=outcome.current_replicas,
            desired_replicas=outcome.desired_replicas,
            action=outcome.action,
            reason=outcome.reason,
        )
        return outcome

    # ── decision ──────────────────────────────────────────────────────

    async def _decide(
        self,
        *,
        heartbeats: list[RuntimeHeartbeat],
        utilization: float,
        current: int,
        held: int,
    ) -> CapacityOutcome:
        def outcome(
            desired: int, action: str, reason: str
        ) -> CapacityOutcome:
            return CapacityOutcome(
                utilization=utilization,
                current_replicas=current,
                desired_replicas=desired,
                action=action,
                reason=reason,
                live_instances=len(heartbeats),
                held_leases=held,
            )

        if utilization >= settings.capacity_scale_up_threshold:
            if current >= settings.capacity_max_replicas:
                return outcome(current, "hold", "at max replicas")
            await self._driver.set_replicas(current + 1)
            return outcome(
                current + 1,
                "scale_up",
                f"utilization {utilization:.0%} >= "
                f"{settings.capacity_scale_up_threshold:.0%}",
            )

        if (
            utilization <= settings.capacity_scale_down_threshold
            and current > settings.capacity_min_replicas
        ):
            # Continue an in-flight drain first: its victim is gone or timing
            # out, and starting a second drain against a different instance
            # would strand the first request's bookkeeping.
            pending = await self._resolve_pending_drains(heartbeats, outcome)
            if pending is not None:
                return pending
            return await self._scale_down(heartbeats, outcome)

        return outcome(current, "hold", "within thresholds")

    async def _scale_down(
        self,
        heartbeats: list[RuntimeHeartbeat],
        outcome: t.Callable[[int, str, str], CapacityOutcome],
    ) -> CapacityOutcome:
        current = await self._driver.current_replicas()
        draining_ids = {hb.runtime_id for hb in heartbeats if hb.draining}
        candidates = [hb for hb in heartbeats if hb.runtime_id not in draining_ids]
        if not candidates:
            return outcome(current, "hold", "every instance is already draining")

        # The emptiest instance is the cheapest victim: fewer leases to
        # hand over, fewer final snapshots to push.
        victim = min(candidates, key=lambda hb: hb.lease_count)

        if victim.lease_count > 0:
            requested = await self._drain_requester(victim.addr)
            if not requested:
                return outcome(
                    current, "hold", f"drain request to {victim.runtime_id} failed"
                )
            self._drain_requested_at[victim.runtime_id] = datetime.now(timezone.utc)
            return outcome(
                current,
                "drain_requested",
                f"asked {victim.runtime_id} "
                f"({victim.lease_count} leases) to drain",
            )

        await self._driver.set_replicas(current - 1)
        return outcome(
            current - 1,
            "scale_down",
            f"utilization low and {victim.runtime_id} holds no leases",
        )

    async def _resolve_pending_drains(
        self,
        heartbeats: list[RuntimeHeartbeat],
        outcome: t.Callable[[int, str, str], CapacityOutcome],
    ) -> t.Optional[CapacityOutcome]:
        """Advance or expire a previously requested drain.

        Returns the tick's outcome when a pending drain decides it (still
        waiting, scaled down, or expired), ``None`` when nothing is pending
        and the caller should start a new drain.
        """
        if not self._drain_requested_at:
            return None

        by_id = {hb.runtime_id: hb for hb in heartbeats}
        for runtime_id, requested_at in list(self._drain_requested_at.items()):
            current = await self._driver.current_replicas()

            if runtime_id not in by_id:
                # The drained instance's heartbeat is gone: the leases were
                # released and takeover already happened elsewhere.
                del self._drain_requested_at[runtime_id]
                await self._driver.set_replicas(current - 1)
                return outcome(
                    current - 1,
                    "scale_down",
                    f"{runtime_id} finished draining",
                )

            if datetime.now(timezone.utc) - requested_at > timedelta(
                seconds=settings.capacity_drain_timeout_seconds
            ):
                # A drain that never completes must not wedge scale-down
                # forever; the SIGTERM drain remains the victim's backstop.
                logger.warning(
                    "lease_controller_drain_timeout",
                    runtime_id=runtime_id,
                    requested_at=requested_at.isoformat(),
                )
                del self._drain_requested_at[runtime_id]
                await self._driver.set_replicas(current - 1)
                return outcome(
                    current - 1,
                    "scale_down",
                    f"{runtime_id} drain timed out; SIGTERM drain is the backstop",
                )

            return outcome(
                current,
                "drain_wait",
                f"waiting for {runtime_id} to finish draining",
            )
        return None

    # ── accounting / publishing / drain I/O ───────────────────────────

    async def _account_usage(self, heartbeats: list[RuntimeHeartbeat]) -> None:
        """Report per-tenant lease counts to the usage bookkeeping."""
        if self._usage_reporter is None:
            return
        counts: dict[str, int] = {}
        for hb in heartbeats:
            for lease_key in hb.leases:
                tenant_id = lease_key.split("/", 1)[0]
                counts[tenant_id] = counts.get(tenant_id, 0) + 1
        try:
            await self._usage_reporter(counts)
        except Exception as exc:  # noqa: BLE001 — accounting must not stop control
            logger.warning("lease_usage_report_failed", error=str(exc))

    async def _publish_quotas(self) -> None:
        if self._quota_publisher is None:
            return
        try:
            await self._quota_publisher(self._client)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "quota_publish_failed",
                error=str(exc),
                effect="existing snapshot lives on until its TTL",
            )

    async def _read_heartbeats(self) -> list[RuntimeHeartbeat]:
        """Every decodable heartbeat behind the runtime index."""
        try:
            members = await self._client.smembers(runtime_index_redis_key())
            heartbeats: list[RuntimeHeartbeat] = []
            for runtime_id in members:
                raw = await self._client.get(runtime_redis_key(runtime_id))
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                heartbeat = decode_runtime_heartbeat(raw)
                if heartbeat is not None:
                    heartbeats.append(heartbeat)
            return heartbeats
        except Exception as exc:  # noqa: BLE001
            logger.warning("lease_controller_heartbeats_unreadable", error=str(exc))
            return []

    @staticmethod
    async def _request_drain(addr: str) -> bool:
        """POST the victim's internal drain endpoint (production default)."""
        url = f"http://{addr.rstrip('/')}/internal/leases/drain"
        try:
            async with httpx.AsyncClient(
                timeout=DRAIN_REQUEST_TIMEOUT_SECONDS
            ) as http:
                response = await http.post(
                    url,
                    headers={"X-Internal-Token": settings.runtime_internal_token},
                )
            return response.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drain_request_failed", addr=addr, error=str(exc)
            )
            return False


# ── background loop (mirrors the environment reaper's shape) ───────────────


class LeaseControllerLoop:
    """Runs :class:`LeaseController` on a timer until stopped."""

    def __init__(self, controller: LeaseController, interval: float) -> None:
        self._controller = controller
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="lease-controller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._controller.run_once()
            except Exception as exc:  # noqa: BLE001 — one bad tick must not kill control
                logger.error("lease_controller_tick_failed", error=str(exc))


_controller: LeaseController | None = None


def get_lease_controller() -> LeaseController:
    """Build (once) the controller with its production collaborators."""
    global _controller
    if _controller is None:
        from datacode_common.redis import get_redis

        from src.capacity.drivers import build_replica_driver
        from src.quota import get_quota_service

        client = get_redis()
        if client is None:
            raise RuntimeError(
                "capacity controller enabled but Redis is unavailable"
            )
        _controller = LeaseController(
            client=client,
            driver=build_replica_driver(),
            usage_reporter=get_quota_service().apply_lease_usage,
            quota_publisher=publish_quotas,
        )
    return _controller
