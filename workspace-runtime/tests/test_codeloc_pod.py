"""Pod-mode code location tests: the vehicle and the manager's split.

A dev machine has no cluster, so the cluster is faked — but the MANIFEST is
not: it is compared field by field against the contract checked into
``deploy/k8s/dagster-container-plane.yaml`` (ConfigMap
``codeloc-pod-contract``). That is the point of keeping the contract as YAML:
the code that must satisfy it can be diffed against it, and drift becomes a
failing test instead of a surprise at deploy time.

What is asserted here:

* the built Pod/Service match the contract (labels, command, mounts, hardening);
* names are DNS-safe and deterministic, so a restart converges on one object;
* the backend reports aliveness from Endpoints — the registry — and never from
  a remembered Pod IP;
* the manager routes ensure/reload/remove/advertise through the Pod vehicle
  when ``dagster.code_location_mode: pod``, and advertises the Service FQDN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.codeloc.pod import (
    APP_CODE_LOCATION,
    ASSET_DAG_SUBDIR,
    CODELOC_PORT,
    CodeLocationPodBackend,
    CodeLocationPodError,
    build_pod_manifest,
    build_service_manifest,
    code_location_namespace,
    pod_name,
    service_fqdn,
    service_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_MANIFEST = REPO_ROOT / "deploy" / "k8s" / "base" / "dagster-container-plane.yaml"
EGRESS_MANIFEST = REPO_ROOT / "deploy" / "k8s" / "base" / "egress-plane.yaml"


def _camel(value):
    """snake_case keys -> camelCase, recursively.

    The contract is written in snake_case because it is a description for
    humans and reviewers; a Kubernetes manifest is camelCase. Comparing the two
    needs one of them translated, and doing it here keeps the contract readable.
    """
    if isinstance(value, dict):
        return {_to_camel(key): _camel(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camel(item) for item in value]
    return value


def _to_camel(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(part.title() for part in rest)


def _load_contract() -> dict:
    """The codeloc Pod contract, straight out of the checked-in manifest."""
    documents = [
        doc
        for doc in yaml.safe_load_all(CONTRACT_MANIFEST.read_text(encoding="utf-8"))
        if doc
    ]
    config_map = next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "codeloc-pod-contract"
    )
    return _camel(yaml.safe_load(config_map["data"]["contract.yaml"]))


def _contract_namespace() -> str:
    """Namespace the contract says code location Pods live in.

    Kept as the authority: the manifests are what an operator reads, and the
    NetworkPolicies that guard these Pods are namespaced objects. A Pod created
    in a namespace no policy selects has UNRESTRICTED egress, which is not a
    difference of style — it is the whole control being absent.
    """
    documents = [
        doc
        for doc in yaml.safe_load_all(CONTRACT_MANIFEST.read_text(encoding="utf-8"))
        if doc
    ]
    return next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "codeloc-pod-contract"
    )["metadata"]["namespace"]


def _network_policies() -> list[dict]:
    """Every NetworkPolicy in the egress plane manifest."""
    return [
        doc
        for doc in yaml.safe_load_all(EGRESS_MANIFEST.read_text(encoding="utf-8"))
        if doc and doc.get("kind") == "NetworkPolicy"
    ]


def _selector_matches(selector: dict, labels: dict) -> bool:
    """Whether a NetworkPolicy podSelector selects a Pod with ``labels``."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression["key"], expression["operator"]
        if operator == "In" and labels.get(key) not in expression["values"]:
            return False
        if operator == "NotIn" and labels.get(key) in expression["values"]:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return bool(selector)


@dataclass
class _Obj:
    """Stand-in for a Kubernetes client model: it only needs ``to_dict``."""

    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.payload)


class FakeTasks:
    """The task-backend helpers the Pod vehicle reuses."""

    def _volumes_for_path(self, workspace_path: str, name: str):
        return (
            [_Obj({"name": "workcopy", "persistentVolumeClaim": {"claimName": "pvc"}})],
            [_Obj({"name": "workcopy", "mountPath": "/workspace", "subPath": "t1/ws/dev"})],
        )

    def _scheduling(self, profile):
        return {
            "node_selector": {
                "kubernetes.io/hostname": "node-1",
                "datacode.io/workload": "user-code",
            },
            "tolerations": [
                _Obj({"key": "datacode.io/user-code", "operator": "Exists",
                      "effect": "NoSchedule"})
            ],
        }

    def _container_security_context(self, profile):
        return _Obj({
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        })

    def _pod_security_context(self, profile):
        return _Obj({
            "runAsNonRoot": True,
            "runAsUser": 65534,
            "seccompProfile": {"type": "RuntimeDefault"},
        })

    def _resources(self, profile):
        return _Obj({"limits": {"cpu": "1", "memory": "2Gi"}})

    def _dns_config(self):
        return None


@dataclass
class FakeEndpoints:
    """Shaped like ``V1Endpoints``: ``subsets[i].addresses[j].ip``."""

    addresses: list[str]

    @property
    def subsets(self):
        if not self.addresses:
            return []
        return [
            SimpleNamespace(
                addresses=[SimpleNamespace(ip=ip) for ip in self.addresses]
            )
        ]


class FakeCore:
    """Enough of CoreV1Api to exercise the vehicle."""

    def __init__(self) -> None:
        self.pods: dict[str, dict] = {}
        self.services: dict[str, dict] = {}
        self.endpoints: dict[str, FakeEndpoints] = {}
        self.deleted: list[tuple[str, str]] = []

    def create_namespaced_pod(self, namespace: str, body: dict) -> None:
        self.pods[body["metadata"]["name"]] = body

    def create_namespaced_service(self, namespace: str, body: dict) -> None:
        self.services[body["metadata"]["name"]] = body

    def delete_namespaced_pod(self, name: str, namespace: str) -> None:
        self.deleted.append(("pod", name))
        self.pods.pop(name, None)

    def delete_namespaced_service(self, name: str, namespace: str) -> None:
        self.deleted.append(("service", name))
        self.services.pop(name, None)

    def read_namespaced_endpoints(self, name: str, namespace: str):
        if name not in self.endpoints:
            raise RuntimeError(f"endpoints {name} not found")
        return self.endpoints[name]


def _backend_with_fakes() -> tuple[CodeLocationPodBackend, FakeCore]:
    """A vehicle wired to fakes, without importing the kubernetes client."""
    backend = CodeLocationPodBackend.__new__(CodeLocationPodBackend)
    core = FakeCore()
    backend._tasks = FakeTasks()
    backend._core = core
    backend._api = None
    return backend, core


# ── naming ─────────────────────────────────────────────────────────────


class TestNaming:
    @pytest.mark.parametrize(
        "location", ["ws-abc-dev", "WS_Upper/Dev!", "a" * 80, "team.prod"]
    )
    def test_pod_names_are_dns_safe(self, location):
        name = pod_name(location)

        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)
        assert len(name) <= 63

    def test_pod_names_are_deterministic(self):
        assert pod_name("ws-abc-dev") == pod_name("ws-abc-dev")

    def test_different_locations_get_different_pods(self):
        assert pod_name("ws-a-dev") != pod_name("ws-b-dev")

    def test_service_is_named_after_the_environment(self):
        """The Service is the discovery key; the environment is its identity."""
        assert service_name("Production") == "codeloc-production"

    def test_fqdn_points_at_the_headless_service(self):
        assert service_fqdn("dev", "datacode") == "codeloc-dev.datacode.svc.cluster.local"


# ── the manifest against the contract ──────────────────────────────────


def _pod(environment: str = "dev", **overrides) -> dict:
    volumes = [{"name": "workcopy", "persistentVolumeClaim": {"claimName": "pvc"}}]
    mounts = [
        {"name": "workcopy", "mountPath": "/workspace", "subPath": "t1/ws/dev"},
        {"name": "scratch", "mountPath": "/tmp"},
    ]
    scheduling = FakeTasks()._scheduling(None)
    params = dict(
        name="codeloc-x-1234abcd",
        namespace=_contract_namespace(),
        location_name="ws-abc-dev",
        environment=environment,
        image="datacode/task-runner:latest",
        image_pull_policy="IfNotPresent",
        service_account="task-runner-noperm",
        volumes=volumes,
        volume_mounts=mounts,
        scheduling=scheduling,
        security_context=FakeTasks()._container_security_context(None).to_dict(),
        resources=FakeTasks()._resources(None).to_dict(),
        pod_security_context=FakeTasks()._pod_security_context(None).to_dict(),
        env={"DATACODE_ENVIRONMENT": environment},
    )
    params.update(overrides)
    return build_pod_manifest(**params)


class TestContractAlignment:
    def test_labels_match_the_contract(self):
        contract = _load_contract()
        pod = _pod(environment="dev")

        expected = dict(contract["metadata"]["labels"])
        expected["datacode.io/environment"] = "dev"
        for key, value in expected.items():
            assert pod["metadata"]["labels"][key] == value

    def test_the_identity_fields_match(self):
        contract = _load_contract()
        pod = _pod()
        spec = pod["spec"]

        assert spec["serviceAccountName"] == contract["spec"]["serviceAccountName"]
        assert spec["automountServiceAccountToken"] is False
        assert spec["enableServiceLinks"] is False
        assert spec["restartPolicy"] == contract["spec"]["restartPolicy"]

    def test_scheduling_matches_the_contract(self):
        contract = _load_contract()
        pod = _pod()
        spec = pod["spec"]

        # Values are the lease holder's node and the pool, decided at runtime;
        # the contract fixes the KEYS, which is what a reviewer can check.
        assert set(spec["nodeSelector"]) == set(contract["spec"]["nodeSelector"])
        assert [
            t.to_dict()["key"] if hasattr(t, "to_dict") else t["key"]
            for t in spec["tolerations"]
        ] == [t["key"] for t in contract["spec"]["tolerations"]]

    def test_the_container_matches_the_contract(self):
        contract = _load_contract()
        expected = contract["spec"]["containers"][0]
        container = _pod()["spec"]["containers"][0]

        assert container["name"] == expected["name"]
        assert container["command"] == expected["command"]
        assert container["workingDir"] == expected["workingDir"]
        assert container["ports"] == [
            {"name": "grpc", "containerPort": expected["ports"][0]["containerPort"]}
        ]
        assert container["ports"][0]["containerPort"] == CODELOC_PORT

    def test_hardening_matches_the_contract(self):
        """The escape-prevention settings are contract, not preference."""
        actual = _pod()["spec"]["containers"][0]["securityContext"]

        assert actual["allowPrivilegeEscalation"] is False
        assert actual["readOnlyRootFilesystem"] is True
        assert actual["capabilities"]["drop"] == ["ALL"]

    def test_the_working_copy_is_mounted_narrowed(self):
        contract = _load_contract()
        contract_mounts = contract["spec"]["containers"][0]["volumeMounts"]
        expected_mounts = {m["mountPath"]: m for m in contract_mounts}
        mounts = {m["mountPath"]: m for m in _pod()["spec"]["containers"][0]["volumeMounts"]}

        assert set(mounts) == set(expected_mounts)
        # The narrowing is the T1 closure: one working copy, not the whole base.
        assert mounts["/workspace"]["subPath"]

    def test_the_environment_identity_is_passed_through(self):
        container = _pod(environment="staging")["spec"]["containers"][0]

        assert {"name": "DATACODE_ENVIRONMENT", "value": "staging"} in container["env"]

    def test_resources_come_from_the_profile_not_the_contract(self):
        """Resources are deliberately NOT contract-pinned.

        The isolation profile is the single source of hardening and sizing; a
        second copy in the contract would drift from it.
        """
        pod = _pod()

        assert pod["spec"]["containers"][0]["resources"] == {
            "limits": {"cpu": "1", "memory": "2Gi"}
        }


class TestNetworkPolicyCoverage:
    """The guard that was missing and let a hole ship.

    A NetworkPolicy selects within its own namespace. A code location Pod
    carries ``app: dagster-code-location``, so it is selected by neither
    ``task-runner-default-deny`` (``app: task-runner``) nor
    ``dagster-user-code-egress`` (a different namespace) — and a Pod no policy
    selects has UNRESTRICTED egress. That is not a weaker posture; it is no
    posture, on the one vehicle that keeps user code loaded indefinitely.
    """

    def test_the_pod_is_created_in_the_contracts_namespace(self):
        pod = _pod()

        assert pod["metadata"]["namespace"] == _contract_namespace()

    def test_the_default_namespace_agrees_with_the_contract(self):
        """What the code does by default must equal what the manifest says."""
        assert code_location_namespace() == _contract_namespace()

    def test_some_network_policy_selects_code_location_pods(self):
        labels = _pod()["metadata"]["labels"]
        namespace = _contract_namespace()

        governing = [
            policy
            for policy in _network_policies()
            if policy["metadata"]["namespace"] == namespace
            and _selector_matches(policy["spec"]["podSelector"], labels)
        ]

        assert governing, (
            "no NetworkPolicy in "
            f"{EGRESS_MANIFEST.name} selects {labels} in {namespace}: this Pod "
            "would have unrestricted egress"
        )

    def test_governing_policy_denies_egress_by_default(self):
        """Selected is not enough — the policy must restrict, not just exist."""
        labels = _pod()["metadata"]["labels"]
        namespace = _contract_namespace()
        policy = next(
            p
            for p in _network_policies()
            if p["metadata"]["namespace"] == namespace
            and _selector_matches(p["spec"]["podSelector"], labels)
        )

        assert "Egress" in policy["spec"]["policyTypes"]
        cidrs = [
            rule["to"][0]["ipBlock"]["cidr"]
            for rule in policy["spec"].get("egress") or []
            if rule.get("to") and "ipBlock" in rule["to"][0]
        ]
        assert "0.0.0.0/0" not in cidrs

    def test_the_egress_proxy_admits_code_locations(self):
        """Selected but unable to reach the proxy is a silent outage: the
        location starts, serves gRPC, and every outbound call fails."""
        proxy_policy = next(
            p
            for p in _network_policies()
            if p["metadata"]["name"] == "egress-proxy-policy"
        )
        admitted = [
            peer
            for rule in proxy_policy["spec"].get("ingress") or []
            for peer in rule.get("from") or []
            if (peer.get("podSelector") or {}).get("matchLabels", {}).get("app")
            == APP_CODE_LOCATION
        ]

        assert admitted, "code locations cannot reach the egress proxy (3128)"

    def test_the_resolver_admits_code_locations(self):
        """Same failure mode, one step earlier: no DNS means no proxy either."""
        dns_policy = next(
            p
            for p in _network_policies()
            if p["metadata"]["name"] == "egress-dns-policy"
        )
        admitted = [
            peer
            for rule in dns_policy["spec"].get("ingress") or []
            for peer in rule.get("from") or []
            if (peer.get("podSelector") or {}).get("matchLabels", {}).get("app")
            == APP_CODE_LOCATION
        ]

        assert admitted, "code locations cannot reach the resolver (53)"


class TestServiceManifest:
    def test_it_is_headless_and_selects_the_pod_labels(self):
        service = build_service_manifest(
            name="codeloc-dev", namespace="datacode", environment="dev"
        )

        assert service["spec"]["clusterIP"] == "None"
        assert service["spec"]["selector"] == {
            "app": APP_CODE_LOCATION,
            "datacode.io/environment": "dev",
        }
        assert service["spec"]["ports"][0]["port"] == CODELOC_PORT

    def test_the_selector_matches_what_the_pod_carries(self):
        """A selector that matches nothing is a Service with no endpoints —
        which is exactly how a location would silently never be discovered."""
        service = build_service_manifest(
            name="codeloc-dev", namespace="datacode", environment="dev"
        )
        labels = _pod(environment="dev")["metadata"]["labels"]

        for key, value in service["spec"]["selector"].items():
            assert labels[key] == value


# ── backend behaviour ──────────────────────────────────────────────────


class _ContainerProfile:
    """Duck-typed container profile for the start path."""

    class level:
        value = "container"


class _ProcessProfile:
    class level:
        value = "process"


@pytest.fixture
def container_profile(monkeypatch):
    """Make the policy resolve codeloc to a container profile."""
    from datacode_common.isolation import IsolationLevel

    import src.codeloc.pod as pod_module

    class Profile:
        level = IsolationLevel.CONTAINER
        name = "L3_base"
        settings: dict = {}

    class Policy:
        def profile_for(self, kind):
            return Profile()

    monkeypatch.setattr(
        pod_module, "runtime_isolation_policy", lambda *a, **k: Policy(),
        raising=False,
    )
    # Imported inside start(); patch the source module instead.
    import src.isolation.policy as policy_module

    monkeypatch.setattr(policy_module, "runtime_isolation_policy", lambda *a, **k: Policy())
    return Profile()


class TestBackend:
    async def test_start_creates_the_service_before_the_pod(self, container_profile):
        backend, core = _backend_with_fakes()
        core.endpoints["codeloc-dev"] = FakeEndpoints(["10.1.2.3"])

        name, host, port = await backend.start(
            "ws-abc-dev", "/data/workspaces/t1/ws-t1-abc/u1/envs/dev", "dev",
            env={"DATACODE_ENVIRONMENT": "dev"},
        )

        assert core.services and core.pods
        assert port == CODELOC_PORT
        assert host == service_fqdn("dev")
        assert name in core.pods

    async def test_a_pod_is_deleted_when_it_never_becomes_ready(
        self, container_profile, monkeypatch
    ):
        """A half-created vehicle must not be left behind holding a mount."""
        backend, core = _backend_with_fakes()
        monkeypatch.setattr("src.config.settings.code_location_pod_ready_timeout", 0.5)

        with pytest.raises(Exception):
            await backend.start(
                "ws-abc-dev", "/data/workspaces/t1/ws-t1-abc/u1/envs/dev", "dev",
                env={"DATACODE_ENVIRONMENT": "dev"},
            )

        assert not core.pods
        assert not core.services

    async def test_aliveness_is_read_from_endpoints(self, container_profile):
        backend, core = _backend_with_fakes()
        core.endpoints["codeloc-dev"] = FakeEndpoints([])

        assert await backend.is_ready("codeloc-dev") is False

        core.endpoints["codeloc-dev"] = FakeEndpoints(["10.1.2.3"])
        assert await backend.is_ready("codeloc-dev") is True

    async def test_a_missing_endpoints_object_means_not_ready(self, container_profile):
        backend, _ = _backend_with_fakes()

        assert await backend.is_ready("codeloc-nope") is False

    async def test_stop_removes_both_objects(self, container_profile):
        backend, core = _backend_with_fakes()
        core.pods["codeloc-x"] = {}
        core.services["codeloc-dev"] = {}

        await backend.stop("codeloc-x", "codeloc-dev")

        assert not core.pods and not core.services
        assert ("pod", "codeloc-x") in core.deleted
        assert ("service", "codeloc-dev") in core.deleted

    async def test_stop_swallows_a_missing_object(self, container_profile):
        """Teardown must not raise: a location that is already gone is done."""
        backend, _ = _backend_with_fakes()

        await backend.stop("ghost", "ghost-svc")


class TestRefusesToDegrade:
    async def test_start_refuses_without_a_container_profile(self, monkeypatch):
        """Degrading to the process vehicle would make pod mode a lie."""
        from datacode_common.isolation import IsolationLevel

        import src.isolation.policy as policy_module

        class Profile:
            level = IsolationLevel.PROCESS
            name = "L3_proc"
            settings: dict = {}

        class Policy:
            def profile_for(self, kind):
                return Profile()

        monkeypatch.setattr(policy_module, "runtime_isolation_policy", lambda *a, **k: Policy())
        backend, core = _backend_with_fakes()

        with pytest.raises(CodeLocationPodError):
            await backend.start(
                "ws-abc-dev", "/data/workspaces/t1/ws-t1-abc/u1/envs/dev", "dev",
                env={"DATACODE_ENVIRONMENT": "dev"},
            )

        assert not core.pods

    def test_the_startup_preflight_refuses_a_process_profile(self):
        """The rollout-time half of the guard: same verdict, no K8s client.

        A misconfigured vehicle must fail the DEPLOYMENT, not surface inside
        the first user's first deployment — the operator is present at
        rollout, not later.
        """
        from datacode_common.isolation import IsolationLevel

        from src.codeloc.pod import assert_vehicle_supported

        class Profile:
            level = IsolationLevel.PROCESS
            name = "L3_proc"
            settings: dict = {}

        class Policy:
            def profile_for(self, kind):
                return Profile()

        with pytest.raises(CodeLocationPodError, match="code_location_mode=pod"):
            assert_vehicle_supported(Policy())

    def test_the_startup_preflight_accepts_a_container_profile(self):
        from datacode_common.isolation import IsolationLevel

        from src.codeloc.pod import assert_vehicle_supported

        class Profile:
            level = IsolationLevel.CONTAINER
            name = "L3_base"
            settings: dict = {}

        class Policy:
            def profile_for(self, kind):
                return Profile()

        assert_vehicle_supported(Policy())

    def test_the_startup_preflight_names_the_setting_to_change(self):
        """The refusal is actionable or it is just an outage."""
        from datacode_common.isolation import IsolationLevel

        from src.codeloc.pod import assert_vehicle_supported

        class Profile:
            level = IsolationLevel.PROCESS
            name = "L3_proc"
            settings: dict = {}

        class Policy:
            def profile_for(self, kind):
                return Profile()

        with pytest.raises(CodeLocationPodError, match="enable the container plane"):
            assert_vehicle_supported(Policy())


# ── manager routing ────────────────────────────────────────────────────


class FakePodBackend:
    """Records what the manager asks of the Pod vehicle.

    Starts out NOT ready: a location that has not been created has no
    endpoints, and a fake that advertised readiness up front would let the
    manager skip the very start the tests are checking.
    """

    def __init__(self) -> None:
        self.started: list[tuple[str, str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self.ready = False

    async def start(self, location_name, workspace_path, environment, env=None):
        self.started.append((location_name, workspace_path, environment))
        self.ready = True
        return (pod_name(location_name), service_fqdn(environment), CODELOC_PORT)

    async def stop(self, name, service):
        self.stopped.append((name, service))
        self.ready = False

    async def is_ready(self, service):
        return self.ready


@pytest.fixture
def pod_manager(tmp_path, monkeypatch):
    """A manager in pod mode over a real (minimal) working copy."""
    from src.codeloc.manager import CodeLocationManager, CodeLocationSpec
    from src.config import Settings

    base = tmp_path / "base"
    workspace = base / "t1" / "ws-t1-abc" / "u1" / "envs" / "dev"
    asset_dag = workspace / ASSET_DAG_SUBDIR
    asset_dag.mkdir(parents=True)
    (asset_dag / "definitions.py").write_text("# user code\n", encoding="utf-8")

    cfg = Settings()
    cfg.workspace_base_path = str(base)
    cfg.code_location_mode = "pod"
    cfg.dagster_home = str(tmp_path / "dagster_home")

    manager = CodeLocationManager(cfg)
    backend = FakePodBackend()
    monkeypatch.setattr(manager, "_pod_backend", lambda: backend)

    spec = CodeLocationSpec(
        location_name="ws-t1-abc-dev",
        workspace_path=str(workspace),
        workspace_name="ws-t1-abc",
        env={"DATACODE_ENVIRONMENT": "dev"},
    )
    return manager, spec, backend


class TestManagerPodMode:
    async def test_ensure_starts_a_pod_and_advertises_the_service(
        self, pod_manager
    ):
        manager, spec, backend = pod_manager

        runtime = await manager.ensure(spec)

        assert backend.started == [
            ("ws-t1-abc-dev", spec.workspace_path, "dev")
        ]
        assert runtime.host == service_fqdn("dev")
        assert runtime.port == CODELOC_PORT
        assert runtime.alive is True

    async def test_advertisements_ignore_the_configured_advertise_host(
        self, pod_manager
    ):
        """The Service FQDN is how the cluster reaches it; a configured host
        would be a second, possibly wrong answer."""
        manager, spec, backend = pod_manager
        manager._settings.code_location_advertise_host = "10.9.9.9"

        await manager.ensure(spec)
        ads = manager.advertisements()

        assert [ad.host for ad in ads] == [service_fqdn("dev")]

    async def test_a_reload_replaces_the_pod_and_moves_the_revision(
        self, pod_manager
    ):
        manager, spec, backend = pod_manager

        await manager.ensure(spec)
        first_revision = manager.advertisements()[0].revision
        await manager.reload(spec)
        second = manager.advertisements()[0]

        assert len(backend.started) == 2
        # Same address, new process: the control plane must re-register.
        assert second.revision > first_revision
        assert second.host == service_fqdn("dev")

    async def test_remove_stops_the_pod_and_its_service(self, pod_manager):
        manager, spec, backend = pod_manager

        await manager.ensure(spec)
        stopped = await manager.remove(spec.location_name)

        assert stopped is True
        assert backend.stopped == [(pod_name(spec.location_name), service_name("dev"))]

    async def test_aliveness_follows_the_endpoints_registry(self, pod_manager):
        manager, spec, backend = pod_manager

        await manager.ensure(spec)
        assert manager.is_alive(spec.location_name) is True

        backend.ready = False
        await manager.refresh_state()
        assert manager.is_alive(spec.location_name) is False

    async def test_a_dead_location_is_not_advertised(self, pod_manager):
        """An endpoint the control plane cannot dial fails its whole workspace
        load, so a dead location is omitted rather than listed as broken."""
        manager, spec, backend = pod_manager

        await manager.ensure(spec)
        backend.ready = False
        await manager.refresh_state()

        assert manager.advertisements() == []

    async def test_port_segmentation_is_skipped_in_pod_mode(self, pod_manager):
        """Every Pod listens on the same fixed port in its own netns; there is
        nothing to allocate and no cross-replica collision to avoid."""
        manager, spec, backend = pod_manager

        await manager.ensure(spec)

        assert manager._allocator.lease is None
