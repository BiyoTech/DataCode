"""Tests for Dagster subprocess lifecycle management and supervision.

Real subprocesses are never spawned: ``ManagedProcess.start`` is patched so we
exercise registry / env / health / restart logic in isolation.
"""

import pytest

from src.config import Settings
from src.core.process_manager import DagsterProcessManager, ManagedProcess


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    """Prevent any real OS process from being created during these tests."""
    monkeypatch.setattr(ManagedProcess, "start", lambda self: None)


def test_managed_process_status_when_not_started():
    status = ManagedProcess(name="p", command=["true"]).status()
    assert status == {
        "name": "p",
        "pid": None,
        "alive": False,
        "uptime_seconds": None,
        "restart_count": 0,
        "abandoned": False,
    }


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
    assert health["supervised"] is False  # no supervisor until start()
    assert any(p["name"] == "worker" for p in health["processes"])


async def test_remote_mode_start_spawns_no_instance_processes():
    """Remote mode: daemon/webserver live remotely; nothing is started here."""
    mgr = DagsterProcessManager(
        Settings(dagster_mode="remote", dagster_remote_graphql_url="http://remote/graphql")
    )
    await mgr.start()
    try:
        # No daemon/webserver processes should have been spawned locally, but the
        # supervisor still runs: code locations are local in remote mode too.
        assert mgr._processes == {}
        assert mgr.health_check()["supervised"] is True
    finally:
        await mgr.stop_all()


# --- Supervision -------------------------------------------------------------


async def test_supervisor_respawns_a_dead_process_and_reattaches_it():
    mgr = DagsterProcessManager(Settings())
    reattached: list[str] = []

    async def on_restart():
        reattached.append("called")

    proc = mgr.spawn("code-location-x", ["x"], on_restart=on_restart)
    # The patched start() never creates a process, so is_alive() is already False.
    await mgr._respawn_dead()

    assert proc.restart_count == 1
    assert reattached == ["called"]


async def test_supervisor_leaves_intentionally_unmanaged_processes_alone():
    mgr = DagsterProcessManager(Settings())
    proc = mgr.spawn("one-shot", ["x"], autorestart=False)
    await mgr._respawn_dead()
    assert proc.restart_count == 0


async def test_supervisor_gives_up_on_a_crash_looping_process():
    """A process that keeps dying must surface, not spin forever."""
    mgr = DagsterProcessManager(Settings(process_max_restarts=2, process_restart_window=300))
    proc = mgr.spawn("code-location-broken", ["x"])

    for _ in range(4):
        await mgr._respawn_dead()

    assert proc.restart_count == 2
    assert proc.abandoned is True
    status = next(p for p in mgr.health_check()["processes"] if p["name"] == proc.name)
    assert status["abandoned"] is True
