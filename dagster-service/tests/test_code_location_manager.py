"""Tests for the workspace -> Code Location manager.

Covers name derivation, the path-traversal guard, workspace.yaml
serialization, and the full ensure/reload lifecycle with the gRPC spawn and
GraphQL reload stubbed out (no real processes / network).
"""

import pytest
import yaml

from src.config import Settings
from src.core.code_location_manager import CodeLocationManager
from src.utils.errors import CodeLocationLoadError, WorkspacePathInvalidError


@pytest.fixture
def env(tmp_path):
    """A settings object + a valid workspace containing asset-dag/definitions.py."""
    base = tmp_path / "workspaces"
    ws = base / "demo-ws"
    (ws / "asset-dag").mkdir(parents=True)
    (ws / "asset-dag" / "definitions.py").write_text("# defs\n")
    home = tmp_path / "dagster_home"
    cfg = Settings(
        dagster_mode="local",
        workspace_base_path=str(base),
        dagster_home=str(home),
    )
    return cfg, str(ws), home


class _FakeProcessManager:
    def __init__(self):
        self.spawned = []

    def spawn(self, name, command, cwd=None, env=None):
        self.spawned.append(name)

    def stop(self, name):
        pass


class _FakeClient:
    def __init__(self):
        self.reloaded = []

    async def reload_code_location(self, location_name):
        self.reloaded.append(location_name)
        return {"name": location_name, "load_status": "LOADED"}


@pytest.fixture
def stub_backends(monkeypatch):
    """Stub the lazily-imported process manager + GraphQL client."""
    fake_pm = _FakeProcessManager()
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "src.core.process_manager.get_process_manager", lambda: fake_pm
    )
    monkeypatch.setattr(
        "src.core.dagster_client.get_dagster_client", lambda: fake_client
    )
    return fake_pm, fake_client


# --- Name derivation ---------------------------------------------------------


def test_location_name_sanitizes_workspace_name():
    mgr = CodeLocationManager(Settings())
    assert mgr.location_name_for("/base/My-WS 01") == "ws_My_WS_01"


# --- Path traversal guard ----------------------------------------------------


def test_definitions_path_rejects_outside_base(tmp_path):
    base = tmp_path / "workspaces"
    base.mkdir()
    mgr = CodeLocationManager(Settings(workspace_base_path=str(base)))
    with pytest.raises(WorkspacePathInvalidError):
        mgr._definitions_path(str(tmp_path / "evil"))


def test_definitions_path_resolves_inside_base(env):
    cfg, ws, _ = env
    mgr = CodeLocationManager(cfg)
    path = mgr._definitions_path(ws)
    assert path.name == "definitions.py"
    assert path.parent.name == "asset-dag"


# --- workspace.yaml ----------------------------------------------------------


def test_ensure_workspace_yaml_writes_empty_load_from(env):
    cfg, _, home = env
    mgr = CodeLocationManager(cfg)
    path = mgr.ensure_workspace_yaml()
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data == {"load_from": []}


# --- Lifecycle: ensure + reload ---------------------------------------------


async def test_ensure_location_missing_definitions_raises(tmp_path):
    base = tmp_path / "workspaces"
    ws = base / "empty-ws"
    ws.mkdir(parents=True)  # no asset-dag/definitions.py
    mgr = CodeLocationManager(
        Settings(workspace_base_path=str(base), dagster_home=str(tmp_path / "dh"))
    )
    with pytest.raises(CodeLocationLoadError):
        await mgr.ensure_location(str(ws))


async def test_ensure_location_spawns_grpc_and_writes_yaml(env, stub_backends):
    cfg, ws, home = env
    fake_pm, fake_client = stub_backends
    mgr = CodeLocationManager(cfg)

    summary = await mgr.ensure_location(ws)

    assert summary["location_name"] == "ws_demo_ws"
    assert summary["port"] == cfg.code_location_port
    # gRPC server spawned + best-effort reload attempted.
    assert fake_pm.spawned
    assert fake_client.reloaded == ["ws_demo_ws"]
    # workspace.yaml now lists the location.
    data = yaml.safe_load((home / "workspace.yaml").read_text())
    assert data["load_from"][0]["grpc_server"]["location_name"] == "ws_demo_ws"


async def test_reload_location_respawns_existing(env, stub_backends):
    cfg, ws, _ = env
    fake_pm, fake_client = stub_backends
    mgr = CodeLocationManager(cfg)

    await mgr.ensure_location(ws)
    fake_pm.spawned.clear()
    result = await mgr.reload_location(ws)

    assert result["reloaded"] is True
    assert fake_pm.spawned  # re-spawned the gRPC server


async def test_ensure_location_reuses_port_across_calls(env, stub_backends):
    cfg, ws, _ = env
    mgr = CodeLocationManager(cfg)
    first = await mgr.ensure_location(ws)
    second = await mgr.ensure_location(ws)
    assert first["port"] == second["port"]
    assert len(mgr.list_locations()) == 1
