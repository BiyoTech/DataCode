"""Warm pool tests — admission, fallback and the invariants it must not break.

The pool is an OPTIMIZATION, and the tests are built around that: every
condition that makes the warm path unavailable must fall back to the one-shot
vehicle, so a task never fails BECAUSE of the pool. What is pinned:

* poolable() refuses non-container profiles and undeclared kinds — a resident
  process on the runtime's own host would be the long-lived hole P6 closed, and
  a pooled WRITE kind would keep a writable warehouse account alive between
  tasks (Q5);
* the pool is keyed by WORKING COPY, so the single-copy subPath mount (T1) is
  preserved — a per-tenant Pod would have to mount a subtree;
* the created Pod carries the label the existing NetworkPolicy selects; a new
  label would mean a Pod no policy governs, i.e. unrestricted egress;
* busy / full / stale-image / transport-failure all return None (fall back),
  while a task that RAN and failed comes back as a failed result;
* a retired Pod is deleted, and a transport failure retires it because it is
  then unknown whether the task ran.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from datacode_common.isolation import IsolationLevel, IsolationProfile
from datacode_common.task_protocol import TaskResult, TaskSpec, TaskStatus
from datacode_common.warm_protocol import (
    ERR_BUSY,
    make_error,
    make_response,
)

from src.tasks.warm_pool import RUNNER_PORT, WarmPool, pool_pod_name

WS = "/data/workspaces/t1/ws-t1-abc/u1/envs/dev"


def _profile(
    level: IsolationLevel = IsolationLevel.CONTAINER, **settings_overrides
) -> IsolationProfile:
    settings = {
        "node_pool": "user-code",
        "credentials": {"warehouse": "scoped_readonly", "platform": "none"},
    }
    settings.update(settings_overrides)
    return IsolationProfile(
        name="L3_base",
        level=level,
        enabled=True,
        allowed_task_kinds=("agent.shell",),
        strength=40,
        settings=settings,
    )


def _spec(task_id: str = "t1", kind: str = "agent.shell") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind=kind,
        workspace_path=WS,
        environment="dev",
        tenant_id="t1",
        workspace_id="ws-t1-abc",
        isolation_profile="L3_base",
        params={"command": "true"},
        timeout_seconds=60,
    )


@pytest.fixture(autouse=True)
def enable_pool(monkeypatch):
    monkeypatch.setattr("src.config.settings.warm_pool_enabled", True)
    monkeypatch.setattr("src.config.settings.warm_pool_kinds", ("agent.shell",))
    monkeypatch.setattr("src.config.settings.warm_pool_max_pods", 2)
    monkeypatch.setattr("src.config.settings.warm_pool_max_tasks", 3)
    monkeypatch.setattr("src.config.settings.warm_pool_idle_seconds", 900)


class FakeRunner:
    """Stands in for a connected resident Pod."""

    def __init__(self, pod_name: str, workspace_path: str, response=None) -> None:
        self.pod_name = pod_name
        self.workspace_path = workspace_path
        self.host = "10.1.2.3"
        self.tasks_served = 0
        self.kinds: tuple[str, ...] = ("agent.shell",)
        self.closed = False
        self.sent: list[TaskSpec] = []
        self.credentials: list[Any] = []
        self._response = response or make_response(
            1, TaskResult(task_id="t1", status=TaskStatus.SUCCEEDED, result={"ok": 1}).to_dict()
        )
        self.raises: Optional[Exception] = None
        self.idle_seconds = 0.0

    def implements(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds

    async def run(self, spec, credentials):
        self.sent.append(spec)
        self.credentials.append(credentials)
        if self.raises is not None:
            raise self.raises
        self.tasks_served += 1
        return self._response

    async def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    """A pool whose Pod creation is faked, plus the fake cluster it talks to."""
    pool = WarmPool()
    created: list[dict] = []
    deleted: list[str] = []
    runners: dict[str, FakeRunner] = {}

    async def fake_start(workspace_path, environment, profile):
        runner = FakeRunner(pool_pod_name(workspace_path), workspace_path)
        created.append({"workspace": workspace_path, "pod": runner.pod_name})
        runners[workspace_path] = runner
        return runner

    async def fake_delete(backend, pod_name):
        deleted.append(pod_name)

    monkeypatch.setattr(pool, "_start", fake_start)
    monkeypatch.setattr(pool, "_delete_pod", fake_delete)
    monkeypatch.setattr(pool, "_backend", lambda: SimpleNamespace())
    return pool, created, deleted, runners


# ── admission policy ────────────────────────────────────────────────────


class TestPoolable:
    def test_a_declared_kind_on_a_container_profile_is_poolable(self, pool):
        p, *_ = pool

        assert p.poolable("agent.shell", _profile()) is True

    def test_a_process_profile_is_never_poolable(self, pool):
        """A resident process on the runtime's own host is the long-lived
        in-process hole P6 exists to close."""
        p, *_ = pool

        assert p.poolable("agent.shell", _profile(level=IsolationLevel.PROCESS)) is False

    def test_an_in_process_profile_is_never_poolable(self, pool):
        p, *_ = pool

        assert (
            p.poolable("agent.shell", _profile(level=IsolationLevel.IN_PROCESS)) is False
        )

    def test_an_undeclared_kind_is_not_poolable(self, pool):
        """Write kinds are excluded by construction: L2 carries a writable
        warehouse account, and a Pod keeping one alive between tasks is exactly
        the coexistence Q5 forbids."""
        p, *_ = pool

        assert p.poolable("sqlmesh.apply", _profile()) is False

    def test_the_pool_can_be_switched_off(self, pool, monkeypatch):
        p, *_ = pool
        monkeypatch.setattr("src.config.settings.warm_pool_enabled", False)

        assert p.poolable("agent.shell", _profile()) is False


class TestKeying:
    def test_pods_are_named_per_working_copy(self):
        """Per copy, NOT per tenant: a per-tenant Pod would have to mount a
        subtree holding several copies, undoing the T1 closure."""
        a = pool_pod_name("/data/workspaces/t1/ws-t1-a/u1/envs/dev")
        b = pool_pod_name("/data/workspaces/t1/ws-t1-a/u2/envs/dev")

        assert a != b

    def test_names_are_deterministic_and_dns_safe(self):
        name = pool_pod_name("/data/workspaces/t1/ws-t1-A/u1/envs/DEV!")

        assert name == pool_pod_name("/data/workspaces/t1/ws-t1-A/u1/envs/DEV!")
        assert name == name.lower()
        assert all(c.isalnum() or c == "-" for c in name)

    async def test_one_pod_is_reused_for_the_same_copy(self, pool):
        p, created, _, _ = pool

        first = await p.acquire(WS, "dev", _profile())
        second = await p.acquire(WS, "dev", _profile())

        assert first is second
        assert len(created) == 1

    async def test_a_second_copy_gets_its_own_pod(self, pool):
        p, created, _, _ = pool
        other = "/data/workspaces/t1/ws-t1-abc/u2/envs/dev"

        await p.acquire(WS, "dev", _profile())
        await p.acquire(other, "dev", _profile())

        assert len(created) == 2


class TestCapacity:
    async def test_above_the_limit_the_caller_falls_back(self, pool):
        """A pool that queues turns a capacity limit into unbounded latency."""
        p, created, _, _ = pool

        await p.acquire("/data/workspaces/t1/ws-t1-abc/u1/envs/a", "dev", _profile())
        await p.acquire("/data/workspaces/t1/ws-t1-abc/u1/envs/b", "dev", _profile())
        third = await p.acquire("/data/workspaces/t1/ws-t1-abc/u1/envs/c", "dev", _profile())

        assert third is None
        assert len(created) == 2

    async def test_a_start_failure_falls_back_instead_of_raising(self, pool, monkeypatch):
        p, _, _, _ = pool

        async def boom(*a, **k):
            raise RuntimeError("cluster unreachable")

        monkeypatch.setattr(p, "_start", boom)

        assert await p.acquire(WS, "dev", _profile()) is None

    async def test_idle_runners_are_retired(self, pool, monkeypatch):
        p, _, deleted, runners = pool

        runner = await p.acquire(WS, "dev", _profile())
        runner.idle_seconds = 10_000  # past the idle cap

        await p.acquire("/data/workspaces/t1/ws-t1-abc/u1/envs/other", "dev", _profile())

        assert runner.closed is True
        assert runner.pod_name in deleted


# ── execution and fallback ──────────────────────────────────────────────


class TestRun:
    async def test_a_poolable_task_runs_on_the_runner(self, pool):
        p, _, _, runners = pool

        result = await p.run(_spec(), _profile(), None)

        assert result is not None
        assert result.status is TaskStatus.SUCCEEDED
        assert [s.task_id for s in runners[WS].sent] == ["t1"]

    async def test_credentials_are_passed_per_task(self, pool):
        """A resident Pod cannot carry a task's token in its environment, so it
        travels with the request."""
        p, _, _, runners = pool
        credentials = SimpleNamespace(env={"DATACODE_TASK_TOKEN": "tok-1"})

        await p.run(_spec(), _profile(), credentials)

        assert runners[WS].credentials == [credentials]

    async def test_a_non_poolable_kind_returns_none(self, pool):
        p, created, _, _ = pool

        assert await p.run(_spec(kind="sqlmesh.apply"), _profile(), None) is None
        assert created == []  # no Pod was started for it

    async def test_a_busy_runner_falls_back(self, pool):
        p, _, _, runners = pool
        await p.acquire(WS, "dev", _profile())
        runners[WS]._response = make_error(1, ERR_BUSY, "already executing")

        assert await p.run(_spec(), _profile(), None) is None

    async def test_a_stale_image_falls_back(self, pool):
        """A runner that does not implement the kind must not be sent the task:
        the failure would read to the user as 'my command is broken'."""
        p, _, _, runners = pool
        await p.acquire(WS, "dev", _profile())
        runners[WS].kinds = ("sqlmesh.plan",)

        assert await p.run(_spec(), _profile(), None) is None

    async def test_a_transport_failure_retires_the_pod_and_falls_back(self, pool):
        """It is unknown whether the task ran, so the Pod is not trusted again;
        the poolable kinds are read-only, which is what makes a retry safe."""
        p, _, deleted, runners = pool
        await p.acquire(WS, "dev", _profile())
        runners[WS].raises = OSError("connection reset")

        result = await p.run(_spec(), _profile(), None)

        assert result is None
        assert runners[WS].pod_name in deleted
        assert p.stats()["pods"] == 0

    async def test_a_task_that_failed_is_a_result_not_a_fallback(self, pool):
        """A failed task is an OUTCOME. Falling back would run it twice."""
        p, _, _, runners = pool
        await p.acquire(WS, "dev", _profile())
        runners[WS]._response = make_error(1, "task_failed", "exit 1")

        result = await p.run(_spec(), _profile(), None)

        assert result is not None
        assert result.status is TaskStatus.FAILED
        assert result.error_code == "task_failed"

    async def test_a_runner_is_retired_after_its_task_budget(self, pool):
        """A task can damage its own interpreter; the next one would inherit it."""
        p, _, deleted, runners = pool
        runner = await p.acquire(WS, "dev", _profile())

        for i in range(3):
            result = await p.run(_spec(f"t{i}"), _profile(), None)
            assert result is not None  # never fails because of retirement

        assert runner.pod_name in deleted
        assert p.stats()["pods"] == 0


class TestRetirement:
    async def test_invalidate_retires_the_copys_runner(self, pool):
        """A git switch or an apply changed the tree the runner sits on."""
        p, _, deleted, runners = pool
        runner = await p.acquire(WS, "dev", _profile())

        await p.invalidate(WS)

        assert runner.closed is True
        assert runner.pod_name in deleted

    async def test_invalidating_an_unknown_copy_is_a_no_op(self, pool):
        p, _, deleted, _ = pool

        await p.invalidate("/data/workspaces/t1/ws-t1-abc/u1/envs/never")

        assert deleted == []

    async def test_shutdown_retires_everything(self, pool):
        p, _, deleted, _ = pool
        await p.acquire(WS, "dev", _profile())
        await p.acquire("/data/workspaces/t1/ws-t1-abc/u1/envs/two", "dev", _profile())

        await p.shutdown()

        assert len(deleted) == 2
        assert p.stats()["pods"] == 0


class TestNetworkPolicyCoverage:
    """Every task vehicle must be selected by exactly one policy.

    A Pod no NetworkPolicy selects has UNRESTRICTED egress — the same hole that
    shipped once with code location Pods. Warm-pool runners are the harder case
    because they need ONE inbound path (the runtime dials them), which the
    one-shot rule must keep denying: so the two vehicles carry different
    `datacode.io/kind` labels and are governed by different policies. These
    tests pin that the labels the code emits still match the selectors.
    """

    @staticmethod
    def _policies() -> list[dict]:
        import yaml

        manifest = (
            Path(__file__).resolve().parents[2]
            / "deploy"
            / "k8s"
            / "base"
            / "egress-plane.yaml"
        )
        return [
            doc
            for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
            if doc and doc.get("kind") == "NetworkPolicy"
        ]

    @staticmethod
    def _selects(selector: dict, labels: dict) -> bool:
        return all(
            labels.get(key) == value
            for key, value in (selector.get("matchLabels") or {}).items()
        )

    def _warm_labels(self) -> dict:
        pool = WarmPool()
        captured: dict = {}

        class FakeApi:
            def __getattr__(self, name):
                return lambda **kwargs: SimpleNamespace(_kind=name, **kwargs)

        backend = SimpleNamespace(
            _api=FakeApi(),
            _core=SimpleNamespace(
                create_namespaced_pod=lambda ns, pod: captured.update(pod=pod)
            ),
            _volumes_for_path=lambda path, name: ([], []),
            _scheduling=lambda profile: {"node_selector": {}, "tolerations": None},
            _resources=lambda profile: {},
            _container_security_context=lambda profile: {},
            _pod_security_context=lambda profile: {},
            _dns_config=lambda: None,
        )
        pool._create_pod(backend, "warm-task-x", WS, "dev", _profile())
        return captured["pod"].metadata.labels

    def test_a_policy_selects_warm_pool_pods(self):
        labels = self._warm_labels()

        governing = [
            policy
            for policy in self._policies()
            if self._selects(policy["spec"]["podSelector"], labels)
        ]

        assert governing, (
            f"no NetworkPolicy selects {labels}: this Pod would have "
            "unrestricted egress"
        )

    def test_the_warm_policy_restricts_egress(self):
        labels = self._warm_labels()
        policy = next(
            p
            for p in self._policies()
            if self._selects(p["spec"]["podSelector"], labels)
        )

        assert "Egress" in policy["spec"]["policyTypes"]
        open_cidrs = [
            rule["to"][0]["ipBlock"]["cidr"]
            for rule in policy["spec"].get("egress") or []
            if rule.get("to") and "ipBlock" in rule["to"][0]
        ]
        assert "0.0.0.0/0" not in open_cidrs

    def test_the_warm_policy_admits_only_the_runtime(self):
        """Another sandbox reaching a runner could feed it tasks against a
        working copy it has no claim on — worse than plain lateral movement."""
        labels = self._warm_labels()
        policy = next(
            p
            for p in self._policies()
            if self._selects(p["spec"]["podSelector"], labels)
        )

        peers = [
            peer
            for rule in policy["spec"].get("ingress") or []
            for peer in rule.get("from") or []
        ]
        assert peers, "the runtime cannot dial the runner: it would never get a task"
        for peer in peers:
            # Cross-namespace and pod-scoped: not "anything in this namespace".
            assert peer.get("namespaceSelector")
            assert peer.get("podSelector")

    def test_one_shot_pods_are_selected_by_the_deny_all_rule(self):
        """The one-shot vehicle must keep taking no inbound traffic at all."""
        one_shot_labels = {"app": "task-runner", "datacode.io/kind": "one-shot"}
        policy = next(
            p
            for p in self._policies()
            if p["metadata"]["name"] == "task-runner-default-deny"
        )

        assert self._selects(policy["spec"]["podSelector"], one_shot_labels)
        assert policy["spec"]["ingress"] == []

    def test_the_deny_all_rule_does_not_capture_warm_runners(self):
        """If it did, the runtime could not dial them and the pool would be a
        silent no-op: Pods start, no task ever reaches them."""
        policy = next(
            p
            for p in self._policies()
            if p["metadata"]["name"] == "task-runner-default-deny"
        )

        assert not self._selects(policy["spec"]["podSelector"], self._warm_labels())

    def test_the_one_shot_backend_emits_the_expected_label(self):
        """The selector above is worthless if the backend stops setting it."""
        from src.tasks.backends.kubernetes import KubernetesBackend

        labels = KubernetesBackend._labels(
            SimpleNamespace(), _spec(), "L3_base"  # type: ignore[arg-type]
        )

        assert labels["app"] == "task-runner"
        assert labels["datacode.io/kind"] == "one-shot"


class TestStats:
    async def test_stats_report_the_pool_state(self, pool):
        p, _, _, _ = pool
        await p.acquire(WS, "dev", _profile())

        stats = p.stats()

        assert stats["enabled"] is True
        assert stats["pods"] == 1
        assert stats["max_pods"] == 2
        assert stats["runners"][0]["tasks_served"] == 0


class TestPodSpec:
    def test_the_pod_carries_the_governed_label(self, monkeypatch):
        """`app: task-runner` is what task-runner-default-deny selects. A new
        label would leave this Pod unselected by any policy — i.e. with
        unrestricted egress."""
        pool = WarmPool()
        captured: dict = {}

        class FakeApi:
            def __getattr__(self, name):
                def build(**kwargs):
                    return SimpleNamespace(_kind=name, **kwargs)

                return build

        class FakeCore:
            def create_namespaced_pod(self, namespace, pod):
                captured["namespace"] = namespace
                captured["pod"] = pod

        backend = SimpleNamespace(
            _api=FakeApi(),
            _core=FakeCore(),
            _volumes_for_path=lambda path, name: ([], []),
            _scheduling=lambda profile: {"node_selector": {}, "tolerations": None},
            _resources=lambda profile: {},
            _container_security_context=lambda profile: {},
            _pod_security_context=lambda profile: {},
            _dns_config=lambda: None,
        )

        pool._create_pod(backend, "warm-task-x", WS, "dev", _profile())

        labels = captured["pod"].metadata.labels
        assert labels["app"] == "task-runner"
        assert labels["datacode.io/kind"] == "warm-pool"

    def test_the_container_serves_on_the_pool_port(self, monkeypatch):
        pool = WarmPool()
        captured: dict = {}

        class FakeApi:
            def __getattr__(self, name):
                def build(**kwargs):
                    return SimpleNamespace(_kind=name, **kwargs)

                return build

        class FakeCore:
            def create_namespaced_pod(self, namespace, pod):
                captured["pod"] = pod

        backend = SimpleNamespace(
            _api=FakeApi(),
            _core=FakeCore(),
            _volumes_for_path=lambda path, name: ([], []),
            _scheduling=lambda profile: {"node_selector": {}, "tolerations": None},
            _resources=lambda profile: {},
            _container_security_context=lambda profile: {},
            _pod_security_context=lambda profile: {},
            _dns_config=lambda: None,
        )

        pool._create_pod(backend, "warm-task-x", WS, "dev", _profile())

        container = captured["pod"].spec.containers[0]
        assert container.command == ["python", "-m", "src.main", "--serve", str(RUNNER_PORT)]
        # The CONTAINER's view of the copy, not the runtime-side path.
        workspace_env = [e for e in container.env if e.name == "DATACODE_WORKSPACE_PATH"]
        assert workspace_env[0].value == "/workspace"
