"""The Dagster instance storage must be provisioned BEFORE any subprocess runs.

The defect these tests pin is a race in Dagster's own first-use provisioning,
triggered deterministically by this service's startup sequence.

Every Dagster SQLite storage stamps its alembic revision the first time it is
opened, choosing the revision by checking whether its data tables already exist.
``create_all()`` sits between those two steps and is DDL, which pysqlite commits
immediately while the stamp commits later. A second process opening the same home
inside that window sees "tables present, version table empty", takes the branch
meant for pre-PR-25740 databases, and stamps a second, unrelated revision.
``alembic_version`` then holds TWO rows, and every later Dagster process — daemon,
webserver, and every code location gRPC server — dies at startup with "Version
table 'alembic_version' has more than one head present".

``DagsterProcessManager.start()`` spawns the daemon and the webserver
sub-milliseconds apart, so a fresh DAGSTER_HOME hit this on every boot. The
user-visible symptom was an asset DAG that stayed permanently empty while the
runtime reported itself healthy, because a code location that cannot start is a
soft failure by design.

``TestStorageIsHealthyBeforeSpawn`` is the behavioural contract: it inspects the
storage at the moment the first subprocess is spawned. ``TestConcurrentOpensAreSafe``
proves the property that makes it work. The rest pin the guard rails.
"""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

from src.codeloc.process import (
    EXTERNAL_STORAGE_KEYS,
    INSTANCE_MIGRATE_TIMEOUT_SECONDS,
    DagsterProcessManager,
)
from src.config import Settings

# The storages whose alembic_version we assert on. Both are provisioned by
# `dagster instance migrate`, and both take the racy branch without it.
_TRACKED_DBS = ("schedules/schedules.db", "history/runs.db")


def _manager(tmp_path, **overrides) -> DagsterProcessManager:
    cfg = Settings()
    cfg.workspace_base_path = str(tmp_path / "base")
    cfg.dagster_home = str(tmp_path / "dagster_home")
    cfg.dagster_mode = "local"
    cfg.dagster_daemon_enabled = True
    cfg.dagster_webserver_enabled = True
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return DagsterProcessManager(cfg)


def _alembic_heads(home: Path) -> dict[str, list[str]]:
    """Revisions stamped in each tracked storage. Missing files are omitted."""
    heads: dict[str, list[str]] = {}
    for rel in _TRACKED_DBS:
        path = home / rel
        if not path.exists():
            continue
        with sqlite3.connect(path) as conn:
            rows = conn.execute("select version_num from alembic_version").fetchall()
        heads[rel] = [row[0] for row in rows]
    return heads


class TestStorageIsHealthyBeforeSpawn:
    """The contract, end to end, with a real migration."""

    async def test_every_storage_has_one_head_when_the_first_process_spawns(
        self, tmp_path, monkeypatch
    ):
        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        observed: list[dict[str, list[str]]] = []
        monkeypatch.setattr(
            manager, "spawn", lambda name, cmd, **kw: observed.append(_alembic_heads(home))
        )

        await manager.start()

        assert observed, "no subprocess was spawned, so nothing was verified"
        for heads in observed:
            assert heads, (
                "instance storage was not provisioned before the spawn: the "
                "subprocess will take Dagster's racy first-use path"
            )
            for db, revisions in heads.items():
                assert len(revisions) == 1, (
                    f"{db} carries {len(revisions)} alembic heads ({revisions}); "
                    "every Dagster process will fail to start"
                )

    async def test_the_migration_precedes_every_spawn(self, tmp_path, monkeypatch):
        """Ordering is the whole mechanism: after a spawn it would be too late."""
        manager = _manager(tmp_path)
        calls: list[str] = []

        async def record_migrate(self) -> bool:
            calls.append("migrate")
            return True

        monkeypatch.setattr(DagsterProcessManager, "_migrate_instance", record_migrate)
        monkeypatch.setattr(manager, "spawn", lambda name, cmd, **kw: calls.append(name))

        await manager.start()

        assert calls[0] == "migrate", f"migration did not come first: {calls}"
        assert calls.count("migrate") == 1

    async def test_remote_mode_migrates_too(self, tmp_path, monkeypatch):
        """Code locations are spawned in remote mode as well, without
        ``--instance-ref``, so they resolve DagsterInstance against this same
        local home — and would race provisioning it."""
        manager = _manager(
            tmp_path,
            dagster_mode="remote",
            dagster_remote_graphql_url="http://remote/graphql",
        )
        calls: list[str] = []

        async def record_migrate(self) -> bool:
            calls.append("migrate")
            return True

        monkeypatch.setattr(DagsterProcessManager, "_migrate_instance", record_migrate)

        await manager.start()

        assert calls == ["migrate"]


class TestConcurrentOpensAreSafe:
    """The property the fix relies on, exercised against real Dagster storage."""

    async def test_parallel_first_opens_leave_a_single_head(self, tmp_path):
        from dagster._core.storage.schedules.sqlite.sqlite_schedule_storage import (
            SqliteScheduleStorage,
        )

        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        home.mkdir(parents=True, exist_ok=True)

        assert await manager._migrate_instance() is True

        # Four openers released together, which is what the daemon, the
        # webserver and the code locations amount to at boot.
        barrier = threading.Barrier(4)
        errors: list[str] = []

        def open_storage() -> None:
            barrier.wait()
            try:
                SqliteScheduleStorage.from_local(str(home / "schedules"))
            except Exception as exc:  # noqa: BLE001 - the failure is the finding
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=open_storage) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        revisions = _alembic_heads(home)["schedules/schedules.db"]
        assert revisions == revisions[:1], (
            f"concurrent opens stamped {revisions}; a second head makes every "
            "Dagster process unstartable"
        )


class TestExternalStorageIsLeftAlone:
    """A configured storage backend belongs to somebody else."""

    @pytest.mark.parametrize("key", EXTERNAL_STORAGE_KEYS)
    async def test_a_configured_storage_backend_is_skipped(
        self, tmp_path, monkeypatch, key
    ):
        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        home.mkdir(parents=True, exist_ok=True)
        (home / "dagster.yaml").write_text(
            f"{key}:\n"
            f"  module: dagster_postgres\n"
            f"  class: PostgresRunStorage\n"
            f"  config:\n"
            f"    postgres_url: postgresql://control-plane:5432/dagster\n",
            encoding="utf-8",
        )
        invoked: list[list[str]] = []

        def record(cmd, **kwargs):
            invoked.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("src.codeloc.process.subprocess.run", record)

        assert manager._external_storage_key(str(home)) == key
        assert await manager._migrate_instance() is False
        # The contract is that no migration is ATTEMPTED. Asserting only on the
        # return value would pass for the wrong reason: a migration that runs
        # and fails also reports False, while having already touched a database
        # owned by the control plane and shared by every replica.
        assert invoked == [], (
            "alembic was run against storage this replica does not own"
        )
        assert _alembic_heads(home) == {}

    async def test_a_dagster_yaml_without_storage_does_not_block_it(self, tmp_path):
        """The shipped template only sets telemetry; it must not be mistaken
        for an external storage declaration."""
        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        home.mkdir(parents=True, exist_ok=True)
        (home / "dagster.yaml").write_text(
            "telemetry:\n  enabled: false\n", encoding="utf-8"
        )

        assert manager._external_storage_key(str(home)) is None

    async def test_an_unreadable_dagster_yaml_is_skipped(self, tmp_path):
        """Unparseable config: we cannot tell which storage it names."""
        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        home.mkdir(parents=True, exist_ok=True)
        (home / "dagster.yaml").write_text("storage: [unclosed\n", encoding="utf-8")

        assert manager._external_storage_key(str(home)) == "unreadable"

    async def test_no_dagster_yaml_means_local_sqlite(self, tmp_path):
        manager = _manager(tmp_path)

        assert manager._external_storage_key(str(tmp_path / "dagster_home")) is None


class TestFailureDegradesInsteadOfRaising:
    """Dagster is supervised, not required: this service also serves SQLMesh,
    Git, files and leases, none of which need it."""

    async def test_a_nonzero_exit_is_reported_not_raised(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        monkeypatch.setattr(
            "src.codeloc.process.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0] if a else [], 1, stdout="", stderr="boom"
            ),
        )

        assert await manager._migrate_instance() is False

    async def test_a_missing_binary_is_reported_not_raised(self, tmp_path, monkeypatch):
        def enoent(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory: 'dagster'")

        manager = _manager(tmp_path)
        monkeypatch.setattr("src.codeloc.process.subprocess.run", enoent)

        assert await manager._migrate_instance() is False

    async def test_a_timeout_is_reported_not_raised(self, tmp_path, monkeypatch):
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="dagster", timeout=1)

        manager = _manager(tmp_path)
        monkeypatch.setattr("src.codeloc.process.subprocess.run", slow)

        assert await manager._migrate_instance() is False

    async def test_a_failed_migration_does_not_stop_the_plane(
        self, tmp_path, monkeypatch
    ):
        """The processes are still started: a corrupted home is a possibility,
        not a certainty, and refusing to boot over it would be worse."""
        manager = _manager(tmp_path)
        spawned: list[str] = []

        async def failing(self) -> bool:
            return False

        monkeypatch.setattr(DagsterProcessManager, "_migrate_instance", failing)
        monkeypatch.setattr(manager, "spawn", lambda name, cmd, **kw: spawned.append(name))

        await manager.start()

        assert spawned, "a failed migration must not suppress the Dagster plane"

    async def test_the_failure_log_names_the_cause_and_the_remediation(
        self, tmp_path, monkeypatch
    ):
        """An error with no way to act on it costs an operator the diagnosis
        this whole mechanism exists to skip."""
        from structlog.testing import capture_logs

        manager = _manager(tmp_path)
        monkeypatch.setattr(
            "src.codeloc.process.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
        )

        with capture_logs() as logs:
            await manager._migrate_instance()

        failures = [e for e in logs if e.get("event") == "dagster_instance_migrate_failed"]
        assert failures, f"the failure was never logged: {logs}"
        entry = failures[0]
        assert entry["log_level"] == "error"
        assert "boom" in entry["error"], "the cause never reached the log"
        assert "alembic_version" in entry["remediation"]
        assert entry["home"] == manager._dagster_home()

    async def test_a_skip_is_logged_with_the_offending_key(self, tmp_path):
        """Skipping is a decision an operator may need to understand."""
        from structlog.testing import capture_logs

        manager = _manager(tmp_path)
        home = Path(manager._dagster_home())
        home.mkdir(parents=True, exist_ok=True)
        (home / "dagster.yaml").write_text(
            "run_storage:\n  postgres:\n    postgres_db: {}\n", encoding="utf-8"
        )

        with capture_logs() as logs:
            await manager._migrate_instance()

        skips = [e for e in logs if e.get("event") == "dagster_instance_migrate_skipped"]
        assert skips, f"the skip was never logged: {logs}"
        assert skips[0]["storage_key"] == "run_storage"


class TestSubprocessInvocation:
    async def test_it_runs_dagsters_own_command(self, tmp_path, monkeypatch):
        """Not a hand-rolled stamp: that would fork the truth about which
        alembic revision is head into this codebase."""
        manager = _manager(tmp_path)
        seen: dict = {}

        def capture(cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("src.codeloc.process.subprocess.run", capture)

        assert await manager._migrate_instance() is True
        assert seen["cmd"] == ["dagster", "instance", "migrate"]

    async def test_it_targets_this_services_home_and_interpreter(
        self, tmp_path, monkeypatch
    ):
        """Same env as every other Dagster subprocess: the home it must
        provision, and a PATH where the console scripts resolve."""
        manager = _manager(tmp_path)
        seen: dict = {}

        def capture(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("src.codeloc.process.subprocess.run", capture)

        await manager._migrate_instance()

        env = seen["env"]
        assert env["DAGSTER_HOME"] == manager._dagster_home()
        assert env["PATH"] == manager.build_dagster_env()["PATH"]
        assert seen["timeout"] == INSTANCE_MIGRATE_TIMEOUT_SECONDS
