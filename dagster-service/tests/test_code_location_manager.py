"""Tests for the Code Location manager (the mechanism behind a deployment).

Covers the path-traversal guard, workspace.yaml serialization, persistent port
assignment and the ensure/reload/remove lifecycle, with the gRPC spawn and the
GraphQL reload stubbed out (no real processes / network).

The location NAME is no longer derived here — it is passed in by the deployment
manager — so these tests assert that the caller's name is what reaches the
process, workspace.yaml and the instance.
"""

from pathlib import Path

import pytest
import yaml

from src.config import Settings
from src.core.code_location_manager import CodeLocationManager, CodeLocationSpec
from src.utils.errors import WorkspacePathInvalidError

LOCATION = "ws_t1_sales__dev_alice"


@pytest.fixture
def env(tmp_path):
    """Settings plus a valid environment working copy on disk."""
    base = tmp_path / "workspaces"
    workspace = base / "t1" / "ws-t1-sales" / "u42" / "envs" / "dev_alice"
    workspace.mkdir(parents=True)
    home = tmp_path / "dagster_home"
    cfg = Settings(
        dagster_mode="local",
        workspace_base_path=str(base),
        dagster_home=str(home),
        process_start_timeout=1,
    )
    return cfg, str(workspace), home


@pytest.fixture
def spec(env):
    _, workspace, _ = env
    return CodeLocationSpec(
        location_name=LOCATION,
        workspace_path=workspace,
        workspace_name="ws-t1-sales",
        env={"AGENTICDATA_ENVIRONMENT": "dev_alice"},
    )


class _FakeProcess:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class _FakeProcessManager:
    def __init__(self):
        self.spawned: list[str] = []
        self.envs: list[dict] = []
        self.processes: dict[str, _FakeProcess] = {}

    def spawn(self, name, command, cwd=None, env=None, autorestart=True, on_restart=None):
        self.spawned.append(name)
        self.envs.append(env or {})
        self.processes[name] = _FakeProcess()

    def stop(self, name):
        self.processes.pop(name, None)

    def get(self, name):
        return self.processes.get(name)


class _FakeClient:
    def __init__(self):
        self.reloaded: list[str] = []

    async def reload_code_location(self, location_name):
        self.reloaded.append(location_name)
        return {"name": location_name, "load_status": "LOADED"}


@pytest.fixture
def stub_backends(monkeypatch):
    """Stub the lazily-imported process manager + GraphQL client.

    ``_wait_grpc_ready`` is stubbed too: no port is ever bound here, and waiting
    for one would only burn the start timeout.
    """
    fake_pm = _FakeProcessManager()
    fake_client = _FakeClient()
    monkeypatch.setattr("src.core.process_manager.get_process_manager", lambda: fake_pm)
    monkeypatch.setattr("src.core.dagster_client.get_dagster_client", lambda: fake_client)

    async def _ready(self, entry):
        return True

    monkeypatch.setattr(CodeLocationManager, "_wait_grpc_ready", _ready)
    return fake_pm, fake_client


# --- Path traversal guard ----------------------------------------------------


def test_definitions_path_rejects_outside_base(tmp_path):
    base = tmp_path / "workspaces"
    base.mkdir()
    mgr = CodeLocationManager(Settings(workspace_base_path=str(base)))
    with pytest.raises(WorkspacePathInvalidError):
        mgr._definitions_path(str(tmp_path / "evil"))


def test_definitions_path_resolves_inside_base(env):
    cfg, workspace, _ = env
    path = CodeLocationManager(cfg)._definitions_path(workspace)
    assert path.name == "definitions.py"
    assert path.parent.name == "asset-dag"


# --- workspace.yaml ----------------------------------------------------------


def test_ensure_workspace_yaml_writes_empty_load_from(env):
    cfg, _, home = env
    path = CodeLocationManager(cfg).ensure_workspace_yaml()
    assert path.exists()
    assert yaml.safe_load(path.read_text()) == {"load_from": []}


# --- Lifecycle ---------------------------------------------------------------


async def test_ensure_uses_the_caller_supplied_location_name(env, spec, stub_backends):
    cfg, _, home = env
    fake_pm, fake_client = stub_backends
    runtime = await CodeLocationManager(cfg).ensure(spec)

    assert runtime.location_name == LOCATION
    assert runtime.port == cfg.code_location_port
    assert fake_pm.spawned == [f"code-location-{LOCATION}"]
    assert fake_client.reloaded == [LOCATION]
    entry = yaml.safe_load((home / "workspace.yaml").read_text())["load_from"][0]
    assert entry["grpc_server"]["location_name"] == LOCATION


async def test_ensure_injects_the_deployment_identity(env, spec, stub_backends):
    cfg, workspace, _ = env
    fake_pm, _ = stub_backends
    await CodeLocationManager(cfg).ensure(spec)

    injected = fake_pm.envs[0]
    assert injected["AGENTICDATA_ENVIRONMENT"] == "dev_alice"
    assert injected["DAGSTER_WORKSPACE_PATH"] == workspace
    assert injected["SQLMESH_SERVICE_URL"]


async def test_ensure_scaffolds_the_asset_dag_project(env, spec, stub_backends):
    cfg, workspace, _ = env

    await CodeLocationManager(cfg).ensure(spec)
    asset_dag = Path(workspace) / "asset-dag"
    assert (asset_dag / "definitions.py").exists()
    # The deployment gate is part of the canonical scaffold.
    assert (asset_dag / "defs" / "deployment.py").exists()


async def test_ensure_does_not_respawn_a_healthy_location(env, spec, stub_backends):
    cfg, _, _ = env
    fake_pm, _ = stub_backends
    mgr = CodeLocationManager(cfg)

    first = await mgr.ensure(spec)
    fake_pm.spawned.clear()
    second = await mgr.ensure(spec)

    assert fake_pm.spawned == []  # idempotent: safe on every page load
    assert first.port == second.port
    assert len(mgr.list()) == 1


async def test_reload_respawns_and_reattaches(env, spec, stub_backends):
    cfg, _, _ = env
    fake_pm, fake_client = stub_backends
    mgr = CodeLocationManager(cfg)

    await mgr.ensure(spec)
    fake_pm.spawned.clear()
    fake_client.reloaded.clear()
    runtime = await mgr.reload(spec)

    assert runtime.location_name == LOCATION
    assert fake_pm.spawned == [f"code-location-{LOCATION}"]
    assert fake_client.reloaded == [LOCATION]


async def test_remove_stops_the_process_and_drops_it_from_workspace_yaml(
    env, spec, stub_backends
):
    cfg, _, home = env
    fake_pm, _ = stub_backends
    mgr = CodeLocationManager(cfg)
    await mgr.ensure(spec)

    assert mgr.remove(LOCATION) is True
    assert fake_pm.get(f"code-location-{LOCATION}") is None
    assert yaml.safe_load((home / "workspace.yaml").read_text()) == {"load_from": []}
    # Idempotent: removing an unknown deployment reports "nothing was running".
    assert mgr.remove(LOCATION) is False


# --- Port persistence --------------------------------------------------------


async def test_ports_survive_a_service_restart(env, spec, stub_backends):
    """A location's port must be stable: workspace.yaml and remote
    registrations reference it, so a silent move invalidates both."""
    cfg, _, _ = env
    first = await CodeLocationManager(cfg).ensure(spec)

    # A fresh manager stands in for a restarted service.
    restarted = CodeLocationManager(cfg)
    assert restarted.port_of(LOCATION) == first.port
    assert (await restarted.ensure(spec)).port == first.port


async def test_distinct_locations_get_distinct_ports(env, spec, stub_backends):
    cfg, workspace, _ = env
    mgr = CodeLocationManager(cfg)
    other = CodeLocationSpec(
        location_name="ws_t1_sales__prod",
        workspace_path=workspace,
        workspace_name="ws-t1-sales",
    )

    first = await mgr.ensure(spec)
    second = await mgr.ensure(other)
    assert first.port != second.port
