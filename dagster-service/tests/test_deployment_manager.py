"""Tests for the deployment manager — one deployment per environment.

The properties worth locking down are the ones that keep environments apart:
identity comes from the working copy (never from the caller), production typing
cannot be borrowed by a branch, schedules default to off outside production, and
every run carries the deployment's tags.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.core import naming
from src.core.code_location_manager import CodeLocationRuntime
from src.core.deployment_manager import DeploymentManager
from src.utils.errors import (
    DeploymentInvalidError,
    DeploymentNotFoundError,
    WorkspacePathInvalidError,
)


class _FakeCodeLocations:
    """Records what the deployment manager asks of the mechanism layer."""

    def __init__(self):
        self.ensured: list[str] = []
        self.reloaded: list[str] = []
        self.removed: list[str] = []
        self.specs: list = []
        self.ports: dict[str, int] = {}
        self._next_port = 4000

    def _runtime(self, spec):
        port = self.ports.setdefault(spec.location_name, self._next_port)
        if port == self._next_port:
            self._next_port += 1
        return CodeLocationRuntime(
            location_name=spec.location_name,
            workspace_path=spec.workspace_path,
            host="0.0.0.0",
            port=port,
            definitions=f"{spec.workspace_path}/asset-dag/definitions.py",
            alive=True,
        )

    async def ensure(self, spec):
        self.ensured.append(spec.location_name)
        self.specs.append(spec)
        return self._runtime(spec)

    async def reload(self, spec):
        self.reloaded.append(spec.location_name)
        self.specs.append(spec)
        return self._runtime(spec)

    def remove(self, location_name):
        self.removed.append(location_name)
        return self.ports.pop(location_name, None) is not None

    def get(self, location_name):
        return None

    def port_of(self, location_name):
        return self.ports.get(location_name)


@pytest.fixture
def env(tmp_path):
    """Settings plus two working copies of the same workspace."""
    base = tmp_path / "workspaces"
    user_root = base / "t1" / "ws-t1-sales" / "u42" / "envs"
    (user_root / "prod").mkdir(parents=True)
    (user_root / "dev_alice").mkdir(parents=True)
    cfg = Settings(workspace_base_path=str(base), dagster_home=str(tmp_path / "dh"))
    return cfg, str(user_root / "prod"), str(user_root / "dev_alice")


@pytest.fixture
def manager(env):
    cfg, _, _ = env
    locations = _FakeCodeLocations()
    return DeploymentManager(cfg, code_locations=locations), locations


# --- Identity and policy -----------------------------------------------------


def test_resolve_derives_identity_from_the_working_copy(manager, env):
    mgr, _ = manager
    _, _, dev = env
    spec = mgr.resolve(dev)

    assert spec.deployment_name == "ws_t1_sales__dev_alice"
    assert (spec.tenant_id, spec.workspace_id, spec.user_id) == ("t1", "ws-t1-sales", "u42")
    assert spec.environment == "dev_alice"


def test_non_production_defaults_to_sandbox_without_schedules(manager, env):
    mgr, _ = manager
    _, _, dev = env
    spec = mgr.resolve(dev)

    assert spec.env_type == naming.ENV_TYPE_SANDBOX
    assert spec.schedules_enabled is False
    assert spec.code_location_env()["AGENTICDATA_SCHEDULES_ENABLED"] == "0"


def test_production_is_scheduled_by_default(manager, env):
    mgr, _ = manager
    _, prod, _ = env
    spec = mgr.resolve(prod)

    assert spec.env_type == naming.ENV_TYPE_PRODUCTION
    assert spec.is_production
    assert spec.schedules_enabled is True
    assert spec.code_location_env()["AGENTICDATA_SCHEDULES_ENABLED"] == "1"


def test_registry_may_grant_schedules_to_a_shared_environment(manager, env):
    mgr, _ = manager
    _, _, dev = env
    spec = mgr.resolve(dev, env_type=naming.ENV_TYPE_SHARED, schedules_enabled=True)

    assert spec.env_type == naming.ENV_TYPE_SHARED
    assert spec.schedules_enabled is True


def test_resolve_confirms_but_never_redirects_the_environment(manager, env):
    """A mismatch means the caller is pointing at another environment's copy."""
    mgr, _ = manager
    _, _, dev = env

    assert mgr.resolve(dev, environment="dev_alice").environment == "dev_alice"
    with pytest.raises(DeploymentInvalidError):
        mgr.resolve(dev, environment="prod")


def test_a_branch_cannot_be_typed_as_production(manager, env):
    """Otherwise the gate would hand it running schedules on production data."""
    mgr, _ = manager
    _, _, dev = env
    with pytest.raises(DeploymentInvalidError):
        mgr.resolve(dev, env_type=naming.ENV_TYPE_PRODUCTION)


def test_unknown_env_type_is_rejected(manager, env):
    mgr, _ = manager
    _, _, dev = env
    with pytest.raises(DeploymentInvalidError):
        mgr.resolve(dev, env_type="staging")


def test_resolve_rejects_a_path_that_is_not_a_working_copy(manager, tmp_path):
    mgr, _ = manager
    with pytest.raises(WorkspacePathInvalidError):
        mgr.resolve(str(tmp_path / "elsewhere"))


def test_run_tags_attribute_a_run_to_its_environment(manager, env):
    mgr, _ = manager
    _, _, dev = env
    assert mgr.resolve(dev).run_tags() == {
        "agenticdata/environment": "dev_alice",
        "agenticdata/workspace_id": "ws-t1-sales",
        "agenticdata/deployment": "ws_t1_sales__dev_alice",
    }


# --- Lifecycle ---------------------------------------------------------------


async def test_ensure_starts_the_location_under_the_deployment_name(manager, env):
    mgr, locations = manager
    _, _, dev = env
    info = await mgr.ensure(mgr.resolve(dev))

    assert locations.ensured == ["ws_t1_sales__dev_alice"]
    assert info.as_dict()["location_name"] == "ws_t1_sales__dev_alice"
    assert info.alive is True
    # The identity travels with the spec, not with the path.
    assert locations.specs[0].env["AGENTICDATA_DEPLOYMENT"] == "ws_t1_sales__dev_alice"


async def test_two_environments_of_one_workspace_get_two_deployments(manager, env):
    mgr, locations = manager
    _, prod, dev = env
    await mgr.ensure(mgr.resolve(prod))
    await mgr.ensure(mgr.resolve(dev))

    assert sorted(locations.ensured) == ["ws_t1_sales__dev_alice", "ws_t1_sales__prod"]
    assert {info.spec.environment for info in mgr.list()} == {"prod", "dev_alice"}


async def test_reload_targets_the_same_deployment(manager, env):
    mgr, locations = manager
    _, _, dev = env
    spec = mgr.resolve(dev)
    await mgr.ensure(spec)
    await mgr.reload(spec)

    assert locations.reloaded == ["ws_t1_sales__dev_alice"]


async def test_destroy_forgets_the_deployment_and_reports_honestly(manager, env):
    mgr, locations = manager
    _, _, dev = env
    spec = mgr.resolve(dev)
    await mgr.ensure(spec)

    assert await mgr.destroy(spec.deployment_name) is True
    assert locations.removed == ["ws_t1_sales__dev_alice"]
    assert mgr.list() == []
    # Nothing was running the second time: report it rather than claim success.
    assert await mgr.destroy(spec.deployment_name) is False


async def test_diff_requires_both_deployments_to_be_loaded(manager, env):
    mgr, _ = manager
    _, _, dev = env
    await mgr.ensure(mgr.resolve(dev))

    with pytest.raises(DeploymentNotFoundError):
        await mgr.diff(
            deployment_name="ws_t1_sales__dev_alice",
            base_deployment_name="ws_t1_sales__prod",
        )


async def test_diff_reports_asset_and_schedule_changes(manager, env, monkeypatch):
    mgr, _ = manager
    _, prod, dev = env
    await mgr.ensure(mgr.resolve(prod))
    await mgr.ensure(mgr.resolve(dev))

    branch = {
        "assets": [
            _asset("orders"),                     # unchanged
            _asset("customers", deps=["orders"]),  # dependency added
            _asset("new_model"),                   # added
        ],
        "schedules": [
            {"name": "daily", "cronSchedule": "0 3 * * *", "pipelineName": "job"},
            {"name": "hourly", "cronSchedule": "0 * * * *", "pipelineName": "job"},
        ],
    }
    base = {
        "assets": [_asset("orders"), _asset("customers"), _asset("legacy")],
        "schedules": [{"name": "daily", "cronSchedule": "0 2 * * *", "pipelineName": "job"}],
    }
    monkeypatch.setattr(
        "src.core.dagster_client.get_dagster_client",
        lambda: _FakeGraphQLClient(
            {"ws_t1_sales__dev_alice": branch, "ws_t1_sales__prod": base}
        ),
    )

    result = await mgr.diff(
        deployment_name="ws_t1_sales__dev_alice",
        base_deployment_name="ws_t1_sales__prod",
    )

    assert result["assets"] == {
        "added": ["new_model"],
        "changed": ["customers"],
        "removed": ["legacy"],
    }
    changes = {change["name"]: change["change"] for change in result["schedule_changes"]}
    assert changes == {"daily": "modified", "hourly": "added"}


def _asset(name: str, deps: list[str] | None = None) -> dict:
    return {
        "assetKey": {"path": [name]},
        "groupName": "default",
        "dependencyKeys": [{"path": [dep]} for dep in (deps or [])],
    }


class _FakeGraphQLClient:
    def __init__(self, by_location: dict):
        self._by_location = by_location

    async def list_assets(self, location, repository):
        return self._by_location[location]["assets"]

    async def list_schedules(self, location, repository):
        return self._by_location[location]["schedules"]
