"""Table-creation ownership (P5 split, enforced by the create_all cleanup).

The invariant this file pins: workspace-runtime's ``init_db`` creates ONLY the
three tables this service owns. The platform metadata tables (workspaces,
environments, user_git_credentials, environment_events) are control-service's,
created exclusively by its migrate step — until the split was enforced, BOTH
services ran ``create_all`` against the same database, and two concurrent
``CREATE TABLE IF NOT EXISTS`` runs can fail inside PostgreSQL's catalog
(``pg_type_typname_nsp_index`` duplicate key): an intermittent boot failure
that looks like nothing else.

This test is what keeps a future model addition or an ``init_db`` edit from
quietly reintroducing the race: an ORM class appearing here is fine (the
repository layer queries through them), but its table must never be created by
this service.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

# Exactly the tables this service owns; anything else in its ORM metadata is a
# mirror of control's and must NOT be created here (see src/workspace/models.py).
RUNTIME_OWNED = {"plan_apply_history", "knowledge_documents", "file_drafts"}
CONTROL_OWNED = {
    "workspaces",
    "environments",
    "environment_events",
    "user_git_credentials",
}


@pytest.fixture()
def metadata_db(tmp_path, monkeypatch):
    """A throwaway SQLite database wired into the runtime's session factory."""
    db_path = tmp_path / "ownership.db"
    monkeypatch.setenv("WORKSPACE_RUNTIME_DB_TYPE", "sqlite")
    monkeypatch.setenv("WORKSPACE_RUNTIME_DB_SQLITE_PATH", str(db_path))

    from src.workspace.database import init_db

    asyncio.run(init_db())
    return db_path


def _tables(db_path) -> set[str]:
    return {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not row[0].startswith(("sqlite_", "alembic"))
    }


class TestRuntimeCreatesOnlyItsOwnTables:
    def test_every_owned_table_exists(self, metadata_db):
        # If one of these is missing, ``owned`` in init_db drifted from the
        # models — fail here rather than at first use in production.
        assert RUNTIME_OWNED <= _tables(metadata_db)

    def test_no_control_plane_table_is_created(self, metadata_db):
        # The load-bearing half: control's tables appearing here means two
        # creators are racing again, and an intermittent boot failure is back.
        created_control_tables = CONTROL_OWNED & _tables(metadata_db)
        assert not created_control_tables, (
            f"runtime created control-owned tables: {sorted(created_control_tables)}; "
            "control-service's migrate step (src/db_migrate.py) is their only creator"
        )

    def test_alembic_is_not_invoked(self, metadata_db):
        # runtime has no alembic chain since P5 moved it to control-service;
        # a stamp here would mean the dead AUTO_MIGRATE branch came back and
        # two services are writing one alembic_version table again.
        assert "alembic_version" not in _tables(metadata_db)
