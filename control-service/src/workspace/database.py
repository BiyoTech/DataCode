"""Database connection and session management.

The engine/session machinery lives in ``datacode_common.database``; this module
holds what is specific to this service: how its URL is built and WHICH TABLES it
creates.

P5: control-service owns the platform's alembic chain (the migration history
sqlmesh-service maintained). The schema is SHARED with workspace-runtime —
both services point at the same database — and table creation is split by
owner: THIS service's ``create_all`` (run at boot and by the migrate step,
``src/db_migrate.py``) creates the control-plane tables, runtime's creates
only the working-copy tables it owns. The migrations evolve the shared shape
from any bootstrap state.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from datacode_common.database import Database
from datacode_common.database.url import build_database_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _build_database_url() -> str:
    return build_database_url(
        db_type=settings.db_type,
        sqlite_path=settings.db_sqlite_path,
        host=settings.db_host,
        port=settings.db_port,
        name=settings.db_name,
        username=settings.db_username,
        password=settings.db_password,
        project_root=_project_root(),
        # Same SQLite file as the legacy service: one shared schema.
        sqlite_default=".data/sqlmesh.db",
    )


# One instance per service. The URL is resolved through the module-level function
# above so tests can repoint it (``monkeypatch.setattr(database,
# "_build_database_url", ...)``) exactly as before.
_db = Database(
    url_factory=lambda: _build_database_url(),
    db_type_factory=lambda: settings.db_type,
    pool_size_factory=lambda: settings.db_pool_size,
    pool_overflow_factory=lambda: settings.db_pool_overflow,
)


def get_engine() -> AsyncEngine:
    return _db.get_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return _db.get_session_factory()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _db.session() as session:
        yield session


def reset_engine() -> None:
    """Drop the cached engine without disposing it (test fixtures)."""
    _db.reset()


async def init_db() -> None:
    # Importing the model modules registers every table on the shared metadata.
    # The environment registry lives in another package, so it must be imported
    # explicitly or ``create_all`` would silently skip its tables.
    from src.environments import models as environment_models  # noqa: F401
    from src.quota import models as quota_models  # noqa: F401
    from src.records import models as record_models  # noqa: F401
    from src.workspace.models import Base

    engine = get_engine()

    # ``create_all`` owns the BASE tables (0001 is an empty baseline on
    # purpose: the ORM metadata is their single source of truth). It is
    # idempotent and never alters existing tables, so it is safe on every
    # database shape — fresh, legacy, or already migrated.
    #
    # This metadata contains ONLY this service's tables: the runtime's live on its
    # own declarative base, which is why the shared column definitions are mixins
    # rather than a shared base (see datacode_common.models.workspace).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if os.getenv("AUTO_MIGRATE", "").lower() in ("1", "true", "yes"):
        # Migrations EVOLVE whatever ``create_all`` left behind (legacy
        # columns, backfilled rows) to head. They are idempotent, so this
        # converges from any bootstrap state.
        from alembic.config import Config

        from alembic import command

        ini_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        cfg = Config(str(ini_path))
        # The ini's script_location is relative to the CWD; pin it to the ini
        # so startup works no matter where the service is launched from.
        cfg.set_main_option("script_location", str(ini_path.parent / "alembic"))
        command.upgrade(cfg, "head")
        logger.info("database_migrated_via_alembic")
        return

    logger.info("database_tables_initialized")


async def close_db() -> None:
    await _db.close()
