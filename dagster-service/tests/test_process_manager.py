"""Tests for Dagster subprocess lifecycle management.

Real subprocesses are never spawned: ``ManagedProcess.start`` is patched so we
exercise registry/env/health logic in isolation.
"""

import pytest

from src.config import Settings
from src.core.process_manager import DagsterProcessManager, ManagedProcess


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    """Prevent any real OS process from being created during these tests."""
    monkeypatch.setattr(ManagedProcess, "start", lambda self: None)


def test_managed_process_status_when_not_started():
    proc = ManagedProcess(name="p", command=["true"])
    status = proc.status()
    assert status == {"name": "p", "pid": None, "alive": False, "uptime_seconds": None}


def test_build_dagster_env_sets_home_and_disables_telemetry():
    mgr = DagsterProcessManager(Settings(dagster_home="/tmp/dh"))
    env = mgr.build_dagster_env()
    assert env["DAGSTER_HOME"].endswith("/tmp/dh")
    assert env["DAGSTER_TELEMETRY_ENABLED"] == "false"


def test_spawn_registers_process_and_stop_removes_it():
    mgr = DagsterProcessManager(Settings())
    mgr.spawn("worker", ["dagster", "api", "grpc"])
    assert mgr.get("worker") is not None
    mgr.stop("worker")
    assert mgr.get("worker") is None


def test_health_check_reports_mode_and_processes():
    mgr = DagsterProcessManager(Settings(dagster_mode="local"))
    mgr.spawn("worker", ["x"])
    health = mgr.health_check()
    assert health["mode"] == "local"
    assert any(p["name"] == "worker" for p in health["processes"])


async def test_remote_mode_start_spawns_no_instance_processes():
    """Remote mode: daemon/webserver live remotely; nothing is started here."""
    mgr = DagsterProcessManager(
        Settings(dagster_mode="remote", dagster_remote_graphql_url="http://remote/graphql")
    )
    await mgr.start()
    # No daemon/webserver processes should have been spawned locally.
    assert mgr._processes == {}
