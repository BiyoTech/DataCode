"""Dagster shape contract (architecture review P0-3).

``conf/workspace-runtime.yaml`` used to say "production forces remote" while no
code did, and the Kubernetes manifest set neither mode. Every runtime Pod then
ran its own daemon (schedules fired once per replica) on a SQLite instance
outside the working-copy volume (run history lost on every restart), with user
code as a child process outside the isolation profile.

These tests pin the startup contract that makes that unbootable, and the
manifest values that must satisfy it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DagsterContractError, Settings  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K8S_BASE = _REPO_ROOT / "deploy" / "k8s" / "base"

_CLUSTER_SHAPE = dict(
    task_backend="kubernetes",
    dagster_mode="remote",
    code_location_mode="pod",
    dagster_remote_graphql_url="http://dagster-webserver:3000/graphql",
)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


class TestClusterShape:
    def test_remote_and_pod_is_accepted(self):
        shape = _settings(**_CLUSTER_SHAPE).dagster_contract()
        assert shape["dagster_mode"] == "remote"
        # Remote mode never runs the instance processes locally, whatever the
        # enable flags say.
        assert shape["daemon_enabled"] is False
        assert shape["webserver_enabled"] is False

    def test_local_mode_is_refused(self):
        with pytest.raises(DagsterContractError, match="once per replica"):
            _settings(**{**_CLUSTER_SHAPE, "dagster_mode": "local"}).dagster_contract()

    def test_process_vehicle_is_refused(self):
        with pytest.raises(DagsterContractError, match="code_location_mode must be 'pod'"):
            _settings(
                **{**_CLUSTER_SHAPE, "code_location_mode": "process"}
            ).dagster_contract()

    def test_remote_without_a_graphql_url_is_refused(self):
        with pytest.raises(DagsterContractError, match="remote_graphql_url"):
            _settings(
                **{**_CLUSTER_SHAPE, "dagster_remote_graphql_url": ""}
            ).dagster_contract()

    def test_every_problem_is_reported_at_once(self):
        """An operator fixing one line per restart is a slow rollout."""
        with pytest.raises(DagsterContractError) as excinfo:
            _settings(
                task_backend="kubernetes",
                dagster_mode="local",
                code_location_mode="process",
            ).dagster_contract()
        message = str(excinfo.value)
        assert "dagster.mode" in message and "code_location_mode" in message


class TestSingleNodeShapes:
    @pytest.mark.parametrize("backend", ["local", "disabled"])
    def test_single_node_backends_keep_local_mode(self, backend):
        shape = _settings(
            task_backend=backend, dagster_mode="local", code_location_mode="process"
        ).dagster_contract()
        assert shape["dagster_mode"] == "local"

    def test_shipped_default_is_bootable(self):
        _settings().dagster_contract()

    def test_unknown_values_are_refused_rather_than_defaulted(self):
        with pytest.raises(DagsterContractError):
            _settings(dagster_mode="hybrid").dagster_contract()
        with pytest.raises(DagsterContractError):
            _settings(code_location_mode="vm").dagster_contract()


class TestManifest:
    """The K8s base must ship a shape the contract accepts, and the plane it needs."""

    @staticmethod
    def _runtime_env() -> dict[str, str]:
        with open(_K8S_BASE / "workspace-runtime.yaml") as handle:
            for doc in yaml.safe_load_all(handle):
                if doc and doc.get("kind") == "StatefulSet":
                    container = doc["spec"]["template"]["spec"]["containers"][0]
                    return {
                        e["name"]: str(e["value"])
                        for e in container["env"]
                        if "value" in e
                    }
        raise AssertionError("no StatefulSet in workspace-runtime.yaml")

    def test_manifest_shape_satisfies_the_contract(self, monkeypatch):
        env = self._runtime_env()
        for name, value in env.items():
            if name.startswith("WORKSPACE_RUNTIME_"):
                monkeypatch.setenv(name, value)
        shape = Settings().dagster_contract()
        assert shape["task_backend"] == "kubernetes"
        assert shape["dagster_mode"] == "remote"
        assert shape["code_location_mode"] == "pod"

    def test_orchestration_plane_is_part_of_the_base(self):
        with open(_K8S_BASE / "kustomization.yaml") as handle:
            resources = yaml.safe_load(handle)["resources"]
        assert "dagster-orchestrator.yaml" in resources

    def test_daemon_is_a_singleton(self):
        """Two daemons fire every schedule twice; this is not a scaling knob."""
        with open(_K8S_BASE / "dagster-orchestrator.yaml") as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d]
        daemon = next(
            d for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "dagster-daemon"
        )
        assert daemon["spec"]["replicas"] == 1
        # A rolling update would briefly run the old and the new daemon.
        assert daemon["spec"]["strategy"]["type"] == "Recreate"

    def test_orchestrator_pods_carry_the_label_the_policies_admit(self):
        with open(_K8S_BASE / "dagster-orchestrator.yaml") as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d]
        for doc in docs:
            if doc["kind"] == "Deployment":
                labels = doc["spec"]["template"]["metadata"]["labels"]
                assert labels.get("app") == "dagster", doc["metadata"]["name"]

    def test_daemon_and_webserver_open_the_same_instance(self):
        """Repeated env blocks (anchors do not cross documents) must not drift."""
        with open(_K8S_BASE / "dagster-orchestrator.yaml") as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d]
        env_of = {
            d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["env"]
            for d in docs
            if d["kind"] == "Deployment"
        }
        assert env_of["dagster-daemon"] == env_of["dagster-webserver"]

    def test_runtime_points_at_the_orchestrator_service(self):
        env = self._runtime_env()
        with open(_K8S_BASE / "dagster-orchestrator.yaml") as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d]
        service = next(
            d for d in docs
            if d["kind"] == "Service" and d["metadata"]["name"] == "dagster-webserver"
        )
        port = service["spec"]["ports"][0]["port"]
        url = env["WORKSPACE_RUNTIME_DAGSTER_REMOTE_GRAPHQL_URL"]
        assert url.startswith("http://dagster-webserver.")
        assert f":{port}/graphql" in url
