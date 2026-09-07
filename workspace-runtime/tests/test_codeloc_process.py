"""Code location PROCESS vehicle tests: the supervisor and the manager's split.

Pod mode has its own suite (test_codeloc_pod.py); this covers the vehicle that
actually runs in dev and on single-node deployments, and the manager's routing
when ``dagster.code_location_mode: process``.

What is pinned here:

* restart budgeting is a SLIDING window — a process that has been healthy for
  days gets its retries back, while one crash-looping right now is abandoned
  instead of respawned forever (a permanent spawn loop hides the real error);
* an explicit spawn replaces a live process and clears the budget, because the
  caller is acting on new information (fresh code, fixed config);
* a code location that dies is detected by the manager through the supervisor,
  not by a liveness flag it remembers;
* the manager advertises the configured host and its persisted port, and only
  advertises locations that are actually alive.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from src.codeloc.manager import CodeLocationManager, CodeLocationSpec
from src.codeloc.process import ManagedProcess
from src.config import Settings

WS_ENV = {"DATACODE_ENVIRONMENT": "dev"}


# ── restart budgeting ───────────────────────────────────────────────────


class TestRestartBudget:
    def test_a_healthy_process_may_be_restarted(self):
        proc = ManagedProcess(name="loc", command=["x"])

        assert proc.restart_allowed(max_restarts=3, window_seconds=60) is True

    def test_the_budget_is_exhausted_by_a_crash_loop(self):
        proc = ManagedProcess(name="loc", command=["x"])
        for _ in range(3):
            proc.note_restart()

        assert proc.restart_allowed(max_restarts=3, window_seconds=60) is False
        assert proc.restart_count == 3

    def test_the_window_slides_so_a_stable_process_recovers_its_retries(self):
        """The point of a window over a total count: a location that crashed
        twice last week is not the same risk as one crashing twice a minute."""
        proc = ManagedProcess(name="loc", command=["x"])
        proc.note_restart()
        proc.note_restart()
        proc._restarts = [time.time() - 3600] * 2  # outside the window

        assert proc.restart_allowed(max_restarts=2, window_seconds=60) is True
        assert proc.restart_count == 0

    def test_old_and_recent_restarts_are_counted_separately(self):
        proc = ManagedProcess(name="loc", command=["x"])
        proc._restarts = [time.time() - 3600, time.time()]  # one stale, one fresh

        assert proc.restart_allowed(max_restarts=1, window_seconds=60) is False
        assert proc.restart_count == 1


class TestManagedProcessStatus:
    def test_a_process_that_was_never_started_is_not_alive(self):
        proc = ManagedProcess(name="loc", command=["x"])

        assert proc.is_alive() is False
        assert proc.status()["pid"] is None
        assert proc.status()["uptime_seconds"] is None

    def test_stopping_a_process_that_never_started_is_safe(self):
        """Teardown paths call this on processes that may not have spawned."""
        ManagedProcess(name="loc", command=["x"]).stop(timeout=1)

    def test_status_reports_the_restart_state(self):
        proc = ManagedProcess(name="loc", command=["x"])
        proc.note_restart()
        proc.abandoned = True

        status = proc.status()

        assert status["name"] == "loc"
        assert status["restart_count"] == 1
        assert status["abandoned"] is True


# ── the manager in process mode ─────────────────────────────────────────


class FakeProcess:
    """Minimal stand-in for a supervised process.

    Mirrors the ``ManagedProcess`` surface the manager actually touches.
    ``exit_code`` is part of it because a code location that died is reported
    with HOW it died — a stub without it would hide the fields that make the
    difference between "import failed" and "the kernel killed it".
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.alive = True
        self.exit_status: Optional[int] = None

    def is_alive(self) -> bool:
        return self.alive

    def exit_code(self) -> Optional[int]:
        return None if self.alive else self.exit_status


class FakeProcessManager:
    """The registry the manager consults for process-mode liveness."""

    def __init__(self) -> None:
        self.spawned: dict[str, FakeProcess] = {}
        self.commands: dict[str, list[str]] = {}
        self.envs: dict[str, dict] = {}
        self.stopped: list[str] = []

    def spawn(self, name, command, *, cwd=None, env=None, on_restart=None):
        self.spawned[name] = FakeProcess(name)
        self.commands[name] = list(command)
        self.envs[name] = dict(env or {})
        return self.spawned[name]

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.spawned.pop(name, None)

    def get(self, name: str):
        return self.spawned.get(name)


@pytest.fixture
def process_manager(tmp_path, monkeypatch):
    """A manager in PROCESS mode with the OS side faked."""
    base = tmp_path / "base"
    workspace = base / "t1" / "ws-t1-abc" / "u1" / "envs" / "dev"
    asset_dag = workspace / "asset-dag"
    asset_dag.mkdir(parents=True)
    (asset_dag / "definitions.py").write_text("# user code\n", encoding="utf-8")

    cfg = Settings()
    cfg.workspace_base_path = str(base)
    cfg.code_location_mode = "process"
    cfg.dagster_home = str(tmp_path / "dagster_home")
    cfg.code_location_advertise_host = "runtime-1.datacode"

    manager = CodeLocationManager(cfg)
    processes = FakeProcessManager()
    monkeypatch.setattr(
        "src.codeloc.process.get_process_manager", lambda: processes
    )
    # Port segmentation needs Redis; the port VALUE is not what these tests are
    # about, so it is pinned.
    monkeypatch.setattr(manager, "_allocate_port", lambda name: 4321)
    monkeypatch.setattr(
        manager, "_ensure_segment", lambda: asyncio.sleep(0)
    )
    monkeypatch.setattr(manager, "_propagate", lambda entry: asyncio.sleep(0))
    # Readiness normally means "the gRPC port accepts connections", which would
    # need a real server; these tests are about routing, so it is stubbed to
    # "ready". The one branch worth measuring for real (a process that died on
    # startup) is tested separately below.
    monkeypatch.setattr(
        manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
    )

    spec = CodeLocationSpec(
        location_name="ws-t1-abc-dev",
        workspace_path=str(workspace),
        workspace_name="ws-t1-abc",
        env=dict(WS_ENV),
    )
    return manager, spec, processes


class TestManagerProcessMode:
    async def test_ensure_spawns_the_grpc_server(self, process_manager):
        manager, spec, processes = process_manager

        runtime = await manager.ensure(spec)

        name = manager._process_name(spec.location_name)
        assert name in processes.spawned
        command = processes.commands[name]
        assert command[:3] == ["dagster", "api", "grpc"]
        assert str(4321) in command
        assert runtime.alive is True

    async def test_the_deployment_identity_reaches_the_child(self, process_manager):
        """The identity is the caller's; the manager passes it through."""
        manager, spec, processes = process_manager

        await manager.ensure(spec)

        env = processes.envs[manager._process_name(spec.location_name)]
        assert env["DATACODE_ENVIRONMENT"] == "dev"

    async def test_an_alive_location_is_not_respawned(self, process_manager):
        """Respawning a healthy location kills in-flight GraphQL calls and
        briefly makes the webserver answer 5xx for it."""
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        await manager.ensure(spec)

        name = manager._process_name(spec.location_name)
        assert processes.commands[name]
        assert manager.advertisements()[0].revision == 1

    async def test_a_dead_location_is_respawned(self, process_manager):
        """The supervisor's job, observed through the manager: a location that
        died is restarted rather than left missing."""
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        processes.spawned[manager._process_name(spec.location_name)].alive = False

        await manager.ensure(spec)

        assert manager.advertisements()[0].revision == 2

    async def test_aliveness_follows_the_process_registry(self, process_manager):
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        assert manager.is_alive(spec.location_name) is True

        processes.spawned[manager._process_name(spec.location_name)].alive = False
        assert manager.is_alive(spec.location_name) is False

    async def test_a_dead_location_is_not_advertised(self, process_manager):
        """An endpoint the control plane cannot dial fails its WHOLE workspace
        load, so a dead location is omitted rather than listed as broken."""
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        processes.spawned[manager._process_name(spec.location_name)].alive = False

        assert manager.advertisements() == []

    async def test_advertisements_use_the_configured_host_and_port(
        self, process_manager
    ):
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        (ad,) = manager.advertisements()

        assert ad.host == "runtime-1.datacode"
        assert ad.port == 4321

    async def test_reload_respawns_and_moves_the_revision(self, process_manager):
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        initial = manager.advertisements()[0].revision
        await manager.reload(spec)

        assert manager.advertisements()[0].revision > initial

    async def test_remove_stops_the_process(self, process_manager):
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        stopped = await manager.remove(spec.location_name)

        assert stopped is True
        assert processes.stopped == [manager._process_name(spec.location_name)]
        assert manager.is_alive(spec.location_name) is False

    async def test_removing_an_unknown_location_reports_false(
        self, process_manager
    ):
        manager, _, _ = process_manager

        assert await manager.remove("never-started") is False

    async def test_readiness_gives_up_at_once_when_the_process_died(
        self, process_manager, monkeypatch
    ):
        """A location whose user code crashed on import will never open its
        port. Polling the full timeout would stall an ensure for 30s per broken
        location; the process being gone is already the answer."""
        manager, spec, processes = process_manager
        await manager.ensure(spec)

        # Only now restore the real readiness check: ensure() above uses the
        # stubbed one, so this measures the "process died" branch alone.
        monkeypatch.setattr(
            manager,
            "_wait_grpc_ready",
            CodeLocationManager._wait_grpc_ready.__get__(manager),
        )
        manager._settings.process_start_timeout = 30
        processes.spawned[manager._process_name(spec.location_name)].alive = False

        started = time.monotonic()
        ready = await manager._wait_grpc_ready(
            manager._locations[spec.location_name]
        )

        assert ready is False
        assert time.monotonic() - started < 5
