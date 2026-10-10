"""The migrate entrypoint every deployment's migrate step runs.

``python -m src.db_migrate`` is what dev's ``run_db_migrations``, the compose
one-shot ``migrate`` service and the Kubernetes ``control-migrate`` Job all
execute — BEFORE any service starts. Three properties are load-bearing:

* it creates EVERY control-plane table (the base tables are ORM-owned and
  alembic's 0001 baseline is empty on purpose, so ``alembic upgrade head``
  alone would create nothing);
* it is IDEMPOTENT — dev runs it on every start and deployments re-run it on
  every rollout;
* it creates NO workspace-runtime table: runtime's init_db owns those, and two
  ``create_all`` runs sharing a database was the concurrent-DDL race this
  entrypoint exists to remove.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.db_migrate import _main

# What the entrypoint must produce. Quota/records tables come from alembic's
# 0005 (idempotent, introspector-style) or from create_all's ORM metadata —
# either way the caller does not care which half made them.
CONTROL_TABLES = {
    "workspaces",
    "environments",
    "environment_events",
    "user_git_credentials",
    # 0006: resumable lifecycle operations (src/operations).
    "workspace_operations",
    "alembic_version",
}
RUNTIME_OWNED = {"plan_apply_history", "knowledge_documents", "file_drafts"}


@pytest.fixture()
def migrate_db(tmp_path, monkeypatch):
    """A throwaway SQLite database, migrated exactly like production would."""
    db_path = tmp_path / "migrate.db"
    monkeypatch.setenv("CONTROL_SERVICE_DB_TYPE", "sqlite")
    monkeypatch.setenv("CONTROL_SERVICE_DB_SQLITE_PATH", str(db_path))
    # A pre-existing value (a replica's setting) must not stop the entrypoint
    # from migrating; _main forces it on.
    monkeypatch.setenv("AUTO_MIGRATE", "0")

    asyncio.run(_main())
    return db_path


def _tables(db_path) -> set[str]:
    return {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not row[0].startswith("sqlite_")
    }


class TestDbMigrate:
    def test_creates_every_control_table(self, migrate_db):
        # Missing one of these means the entrypoint silently degraded to
        # alembic-only, and the FIRST service boot against that database fails
        # with "no such table" — hours away from the change that caused it.
        missing = CONTROL_TABLES - _tables(migrate_db)
        assert not missing, f"migrate step did not create: {sorted(missing)}"

    def test_creates_no_runtime_table(self, migrate_db):
        created = RUNTIME_OWNED & _tables(migrate_db)
        assert not created, (
            f"migrate step created runtime-owned tables: {sorted(created)}; "
            "runtime's init_db is their only creator"
        )

    def test_is_idempotent(self, migrate_db, tmp_path):
        # Dev runs this on every start and deployments on every rollout; a
        # second run must neither fail nor duplicate anything.
        before = _tables(migrate_db)
        asyncio.run(_main())
        assert _tables(migrate_db) == before
