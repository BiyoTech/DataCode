"""Regression tests for the alembic migration chain.

A production incident motivated these: a database bootstrapped with
``create_all`` (``AUTO_MIGRATE`` unset) keeps its legacy ``workspaces`` shape
forever — ``create_all`` never alters existing tables — and the first ORM query
touching ``production_env`` then fails with "Unknown column". The migration
chain must therefore upgrade EVERY real-world bootstrap shape to head:

* legacy schema (``git_branch``, no registry tables) — the incident's shape;
* ``create_all``-booted schema (new tables/columns already present, but no
  ``alembic_version`` and no backfilled production rows);
* empty database (the from-scratch chain).

The chain is exercised against SQLite: the backfill is deliberately written in
Python (not ``INSERT ... SELECT``) precisely so its behaviour is identical on
every dialect, so proving it on SQLite proves it on MySQL/PostgreSQL.

Downgrade is pinned too: a rollback must restore a loadable legacy shape.

Handed over from the legacy sqlmesh-service chain (P5); head is now
0005_control_plane_tables, which adds the control-plane bookkeeping tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from datacode_common import naming

from alembic import command
from src.workspace.models import Base

HEAD = "0005_control_plane_tables"

# The pre-environment ``workspaces`` shape: ``git_branch`` instead of the
# production/default environment columns, no registry tables at all.
_LEGACY_WORKSPACES_DDL = """
CREATE TABLE workspaces (
    id VARCHAR(128) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    dialect VARCHAR(32) NOT NULL DEFAULT 'duckdb',
    git_repo_url VARCHAR(512),
    git_branch VARCHAR(64) NOT NULL DEFAULT 'main',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    last_active_at DATETIME NOT NULL
)
"""

_CREATED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)


def _alembic_config(db_path: Path) -> Config:
    """Alembic config bound to ``db_path`` via the service's own env vars.

    ``alembic/env.py`` builds the URL from ``Settings``, which reads
    ``CONTROL_SERVICE_*`` env vars at call time — the same indirection the
    production command line uses.
    """
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    # Absolute script_location: the ini's relative value only resolves when the
    # process happens to run from the service directory.
    cfg.set_main_option("script_location", str(ini_path.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


@pytest.fixture()
def migration_db(tmp_path, monkeypatch):
    """A temp SQLite database the alembic env resolves to, plus its engine.

    Returns a callable: ``db(prepare=None)`` yields ``(path, engine)`` after
    optionally running ``prepare(engine)`` to set up a bootstrap shape.
    """
    db_path = tmp_path / "migration.db"
    # The URL is resolved through env vars on every command; DATABASE_URL would
    # bypass them entirely.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CONTROL_SERVICE_DB_TYPE", "sqlite")
    monkeypatch.setenv("CONTROL_SERVICE_DB_SQLITE_PATH", str(db_path))

    def _prepare(prepare=None):
        engine = sa.create_engine(f"sqlite:///{db_path}")
        if prepare is not None:
            prepare(engine)
        engine.dispose()
        return db_path

    return _prepare


def _seed_legacy_workspaces(engine: sa.Engine) -> None:
    """One active and one deleted workspace on the legacy shape."""
    with engine.begin() as conn:
        conn.execute(sa.text(_LEGACY_WORKSPACES_DDL))
        for workspace_id, status in (
            ("ws-t1-legacy", "active"),
            ("ws-t1-gone", "deleted"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO workspaces (id, tenant_id, owner_user_id, name,"
                    " dialect, git_branch, status, created_at, updated_at,"
                    " last_active_at)"
                    " VALUES (:id, 't1', 'alice', :id, 'duckdb', 'main',"
                    " :status, :at, :at, :at)"
                ),
                {"id": workspace_id, "status": status, "at": _CREATED_AT},
            )


def test_upgrade_legacy_schema(migration_db):
    """The incident's shape: legacy workspaces, no registry tables, no stamp.

    ``upgrade head`` must add the environment columns, drop ``git_branch``,
    backfill a production environment for every live workspace (and only
    those), create the P5 control-plane tables, and leave the chain stamped at
    head.
    """
    db_path = migration_db(prepare=_seed_legacy_workspaces)
    command.upgrade(_alembic_config(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        workspace_columns = _columns(engine, "workspaces")
        assert {"production_env", "default_env"} <= workspace_columns
        assert "git_branch" not in workspace_columns

        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, name, branch, env_type, status, deployment_name,"
                    " schedules_enabled, ttl_hours FROM environments"
                )
            ).mappings().all()
            assert [row["id"] for row in rows] == ["ws-t1-legacy:prod"]

            row = rows[0]
            assert row["name"] == naming.PRODUCTION_ENV
            assert row["branch"] == naming.branch_of(naming.PRODUCTION_ENV)
            assert row["env_type"] == naming.ENV_TYPE_PRODUCTION
            assert row["status"] == naming.STATUS_ACTIVE
            assert row["deployment_name"] == naming.deployment_name("ws-t1-legacy", "prod")
            assert row["schedules_enabled"] == 1
            assert row["ttl_hours"] == 0

            tables = _table_names(engine)
            assert "environment_events" in tables
            assert {
                "tenant_quotas",
                "tenant_usage",
                "task_records",
                "lease_history",
            } <= tables
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert version == HEAD
    finally:
        engine.dispose()


def test_upgrade_create_all_booted_schema(migration_db):
    """A ``create_all``-booted database: schema ahead of alembic, no stamp.

    Every object 0003/0004/0005 would create already exists (``create_all``
    made them), so the migration must skip the DDL, still backfill the
    production environments, and stamp head without touching existing rows.
    """
    def prepare(engine: sa.Engine) -> None:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO workspaces (id, tenant_id, owner_user_id, name,"
                    " dialect, production_env, default_env, status, created_at,"
                    " updated_at, last_active_at)"
                    " VALUES ('ws-t1-modern', 't1', 'alice', 'modern', 'duckdb',"
                    " 'prod', 'prod', 'active', :at, :at, :at)"
                ),
                {"at": _CREATED_AT},
            )

    db_path = migration_db(prepare=prepare)
    command.upgrade(_alembic_config(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            # The pre-existing workspace row was left untouched, and its
            # production environment — the part create_all can never provide —
            # was backfilled.
            environments = conn.execute(
                sa.text("SELECT id FROM environments")
            ).scalars().all()
            assert environments == ["ws-t1-modern:prod"]
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert version == HEAD
    finally:
        engine.dispose()


def test_chain_without_base_tables_creates_only_registry(migration_db):
    """Standalone alembic on an empty database creates ONLY the registry and
    the control-plane bookkeeping tables.

    The chain's contract: base tables (``workspaces``, ...) are owned by the
    ORM metadata and created by ``init_db``'s ``create_all``; migrations evolve
    an existing schema, they never bootstrap it. A fresh deployment therefore
    runs ``create_all`` before ``upgrade head`` — the flow the next test pins.
    """
    db_path = migration_db()
    command.upgrade(_alembic_config(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            tables = _table_names(engine)
            assert {"environments", "environment_events", "alembic_version"} <= tables
            assert {
                "tenant_quotas",
                "tenant_usage",
                "task_records",
                "lease_history",
            } <= tables
            assert "workspaces" not in tables
            assert "last_applied_dirty" in _columns(engine, "environments")
            assert conn.execute(sa.text("SELECT COUNT(*) FROM environments")).scalar() == 0
    finally:
        engine.dispose()


def test_fresh_bootstrap_via_create_all_then_upgrade(migration_db):
    """The ``init_db`` AUTO_MIGRATE flow on a fresh database.

    ``create_all`` builds the full current-shape schema, then ``upgrade head``
    stamps the chain without touching anything — the registry is empty because
    there is nothing to backfill yet.
    """
    db_path = migration_db(prepare=lambda engine: Base.metadata.create_all(engine))
    command.upgrade(_alembic_config(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            tables = _table_names(engine)
            assert {
                "workspaces",
                "environments",
                "environment_events",
                "alembic_version",
            } <= tables
            assert {"production_env", "default_env"} <= _columns(engine, "workspaces")
            assert conn.execute(sa.text("SELECT COUNT(*) FROM environments")).scalar() == 0
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert version == HEAD
    finally:
        engine.dispose()


def test_repeated_upgrade_is_a_noop(migration_db):
    """A second ``upgrade head`` must not fail nor duplicate backfill rows."""
    db_path = migration_db(prepare=_seed_legacy_workspaces)
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM environments")).scalar() == 1
    finally:
        engine.dispose()


def test_downgrade_restores_legacy_shape(migration_db):
    """Downgrade drops the control-plane tables, then the registry, and
    restores a loadable legacy workspace."""
    db_path = migration_db(prepare=_seed_legacy_workspaces)
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0004_environment_applied_dirty")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    assert "tenant_quotas" not in _table_names(engine)
    assert "lease_history" not in _table_names(engine)
    engine.dispose()

    command.downgrade(cfg, "0003_environments")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    assert "last_applied_dirty" not in _columns(engine, "environments")
    engine.dispose()

    command.downgrade(cfg, "base")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        assert "environments" not in _table_names(engine)
        assert "environment_events" not in _table_names(engine)
        workspace_columns = _columns(engine, "workspaces")
        assert "git_branch" in workspace_columns
        assert "production_env" not in workspace_columns
    finally:
        engine.dispose()
