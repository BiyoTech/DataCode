"""LeaseController tests (design 06 P8 — capacity orchestration).

The controller's DECISION LOGIC is what needs pinning, not Kubernetes: the
driver is injected, so every rule below runs without a cluster —

* scale up at the high threshold, respecting the max bound;
* scale down is DRAIN-FIRST: an instance holding leases is asked to drain and
  the count shrinks only once its heartbeat disappears (or the drain expires,
  with the SIGTERM drain as the documented backstop);
* hysteresis bounds and the min floor;
* per-tenant usage from the heartbeats reaches the quota bookkeeping, and the
  quota snapshot is re-published every tick.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.capacity.lease_controller import (
    LeaseController,
    LoggingReplicaDriver,
)
from src.config import settings


class FakeRedis:
    """Async client honouring the operations the controller performs."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    async def set(self, key, value, ex=None):
        self._values[key] = value
        return True

    async def get(self, key):
        return self._values.get(key)

    async def smembers(self, key):
        return set(self._sets.get(key, set()))


def _heartbeat(
    client: FakeRedis,
    runtime_id: str,
    *,
    lease_count: int = 0,
    max_leases: int = 10,
    draining: bool = False,
    leases: list[str] | None = None,
) -> None:
    from datacode_common.lease import (
        runtime_index_redis_key,
        runtime_redis_key,
    )

    client._values[runtime_redis_key(runtime_id)] = json.dumps(
        {
            "runtime_id": runtime_id,
            "addr": f"{runtime_id}.svc:8002",
            "lease_count": lease_count,
            "max_leases": max_leases,
            "memory_used_ratio": 0.3,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "draining": draining,
            "leases": leases or [],
        }
    )
    client._sets.setdefault(runtime_index_redis_key(), set()).add(runtime_id)


class _Recorder:
    """Captures the injected collaborators' calls."""

    def __init__(self) -> None:
        self.usage_reports: list[dict[str, int]] = []
        self.publish_calls = 0
        self.drain_targets: list[str] = []
        self.drain_ok = True

    async def report_usage(self, counts: dict[str, int]) -> None:
        self.usage_reports.append(counts)

    async def publish(self, client) -> int:
        self.publish_calls += 1
        return 1

    async def request_drain(self, addr: str) -> bool:
        self.drain_targets.append(addr)
        return self.drain_ok


def _controller(client, driver, recorder) -> LeaseController:
    return LeaseController(
        client=client,
        driver=driver,
        usage_reporter=recorder.report_usage,
        quota_publisher=recorder.publish,
        drain_requester=recorder.request_drain,
    )


@pytest.fixture(autouse=True)
def _capacity_settings(monkeypatch):
    """Deterministic thresholds; restored after each test."""
    monkeypatch.setattr(settings, "capacity_scale_up_threshold", 0.85)
    monkeypatch.setattr(settings, "capacity_scale_down_threshold", 0.30)
    monkeypatch.setattr(settings, "capacity_min_replicas", 1)
    monkeypatch.setattr(settings, "capacity_max_replicas", 10)
    monkeypatch.setattr(settings, "capacity_drain_timeout_seconds", 600)


class TestScaleUp:
    async def test_high_utilization_scales_up(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=2)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=9, max_leases=10)

        outcome = await _controller(client, driver, recorder).run_once()

        assert outcome.action == "scale_up"
        assert outcome.desired_replicas == 3
        assert await driver.current_replicas() == 3

    async def test_max_replicas_is_respected(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=10)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=10, max_leases=10)

        outcome = await _controller(client, driver, recorder).run_once()

        assert outcome.action == "hold"
        assert outcome.desired_replicas == 10


class TestScaleDown:
    async def test_idle_empty_instance_is_removed_directly(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=0, max_leases=10)

        outcome = await _controller(client, driver, recorder).run_once()

        assert outcome.action == "scale_down"
        assert outcome.desired_replicas == 2
        assert recorder.drain_targets == []  # nothing to drain

    async def test_loaded_instance_is_drained_first(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=1, max_leases=10, leases=["t1/ws/u1/dev"])

        outcome = await _controller(client, driver, recorder).run_once()

        assert outcome.action == "drain_requested"
        assert outcome.desired_replicas == 3  # unchanged while draining
        assert recorder.drain_targets == ["rt-a.svc:8002"]

    async def test_drained_instance_disappearing_finishes_the_scale_down(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        controller = _controller(client, driver, recorder)
        _heartbeat(client, "rt-a", lease_count=1, leases=["t1/ws/u1/dev"])

        await controller.run_once()  # drain_requested
        # The victim finished: its heartbeat is gone.
        client._sets.clear()
        client._values.clear()
        _heartbeat(client, "rt-b", lease_count=0)

        outcome = await controller.run_once()

        assert outcome.action == "scale_down"
        assert outcome.desired_replicas == 2
        assert await driver.current_replicas() == 2

    async def test_drain_in_progress_waits(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        controller = _controller(client, driver, recorder)
        _heartbeat(client, "rt-a", lease_count=2, leases=["t1/ws/u1/dev", "t1/ws/u2/dev"])

        await controller.run_once()  # drain_requested
        # Still draining (heartbeat present, draining flag now true).
        client._values.clear()
        _heartbeat(client, "rt-a", lease_count=2, draining=True,
                   leases=["t1/ws/u1/dev", "t1/ws/u2/dev"])

        outcome = await controller.run_once()

        assert outcome.action == "drain_wait"
        assert await driver.current_replicas() == 3

    async def test_drain_timeout_expires_and_scales_down(self, monkeypatch):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        controller = _controller(client, driver, recorder)
        _heartbeat(client, "rt-a", lease_count=1, leases=["t1/ws/u1/dev"])

        await controller.run_once()  # drain_requested
        # Backdate the request past the timeout; the victim is still there.
        controller._drain_requested_at["rt-a"] = datetime.now(timezone.utc) - timedelta(
            seconds=settings.capacity_drain_timeout_seconds + 1
        )

        outcome = await controller.run_once()

        assert outcome.action == "scale_down"
        assert await driver.current_replicas() == 2

    async def test_min_replicas_is_the_floor(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=1)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=0, max_leases=10)

        outcome = await _controller(client, driver, recorder).run_once()

        assert outcome.action == "hold"
        assert outcome.reason == "within thresholds"

    async def test_draining_instances_are_never_victims_again(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=0, draining=True)
        _heartbeat(client, "rt-b", lease_count=0)

        outcome = await _controller(client, driver, recorder).run_once()

        # rt-b is the only non-draining candidate, and it is empty.
        assert outcome.action == "scale_down"
        assert recorder.drain_targets == []


class TestAccounting:
    async def test_per_tenant_usage_reaches_the_reporter(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=1)
        recorder = _Recorder()
        _heartbeat(
            client, "rt-a", lease_count=2,
            leases=["t1/ws/u1/dev", "t2/ws/u2/dev"],
        )
        _heartbeat(client, "rt-b", lease_count=1, leases=["t1/ws/u3/prod"])

        await _controller(client, driver, recorder).run_once()

        assert recorder.usage_reports == [{"t1": 2, "t2": 1}]

    async def test_quota_snapshot_is_published_every_tick(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=1)
        recorder = _Recorder()
        _heartbeat(client, "rt-a", lease_count=0)
        controller = _controller(client, driver, recorder)

        await controller.run_once()
        await controller.run_once()

        assert recorder.publish_calls == 2

    async def test_reporting_failure_does_not_stop_control(self):
        client = FakeRedis()
        driver = LoggingReplicaDriver(replicas=1)
        recorder = _Recorder()

        async def _broken(counts):
            raise RuntimeError("database is unreachable")

        controller = LeaseController(
            client=client,
            driver=driver,
            usage_reporter=_broken,
            quota_publisher=recorder.publish,
            drain_requester=recorder.request_drain,
        )
        _heartbeat(client, "rt-a", lease_count=1, leases=["t1/ws/u1/dev"])

        # The tick completes (a hold decision) despite the broken reporter.
        outcome = await controller.run_once()
        assert outcome.action == "hold"


class TestUnreadableHeartbeats:
    """Redis unreadable must mean "no verdict", never "the fleet is idle".

    The controller derives every input from the heartbeat stream. When that
    read failed it used to yield an empty list, which is indistinguishable
    from a healthy-but-idle fleet: utilization 0.0 drove scale-down, and a
    pending drain's victim looked like it had finished draining. A Redis blip
    therefore shrank a fleet whose replicas were all serving leases.
    """

    class _BrokenRedis(FakeRedis):
        async def smembers(self, key):
            raise ConnectionError("redis is unreachable")

    async def test_no_scaling_decision_is_made(self):
        driver = LoggingReplicaDriver(replicas=4)
        recorder = _Recorder()

        outcome = await _controller(self._BrokenRedis(), driver, recorder).run_once()

        assert outcome.action == "hold"
        assert outcome.reason == "heartbeats unreadable"
        assert outcome.desired_replicas == 4
        assert await driver.current_replicas() == 4

    async def test_a_pending_drain_is_not_mistaken_for_a_finished_one(self):
        """The victim's heartbeat is "unknown", not "gone"."""
        driver = LoggingReplicaDriver(replicas=3)
        recorder = _Recorder()
        controller = _controller(self._BrokenRedis(), driver, recorder)
        controller._drain_requested_at["rt-victim"] = datetime.now(timezone.utc)

        outcome = await controller.run_once()

        assert outcome.action == "hold"
        assert await driver.current_replicas() == 3
        # Still pending: the drain is resolved once heartbeats are readable.
        assert "rt-victim" in controller._drain_requested_at

    async def test_usage_and_quotas_are_not_rewritten_from_nothing(self):
        """Zero usage from an unreadable stream would erase every tenant's count."""
        driver = LoggingReplicaDriver(replicas=2)
        recorder = _Recorder()

        await _controller(self._BrokenRedis(), driver, recorder).run_once()

        assert recorder.usage_reports == []
        assert recorder.publish_calls == 0
