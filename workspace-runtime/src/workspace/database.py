"""Database connection and session management.

The engine/session machinery lives in ``datacode_common.database``; this module
holds what is specific to this service: how its URL is built and WHICH TABLES it
creates. That second part is deliberately not shared — see ``init_db`` below.
"""

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
        sqlite_default=".data/sqlmesh.db",
    )


# One instance per service. The URL is resolved through the module-level function
# above so tests can repoint it without touching the shared layer.
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
    """Create ONLY the tables this service owns.

    Ownership: control-service owns the platform metadata tables
    (``workspaces``, ``environments``, ``user_git_credentials``,
    ``environment_events``) — its ORM metadata is their single source of truth
    and its migrate step is the only thing that creates them. This service owns
    and creates ``plan_apply_history``, ``knowledge_documents`` and
    ``file_drafts``.

    Why the split matters: both services point at the SAME database, and two
    concurrent ``CREATE TABLE IF NOT EXISTS`` runs can fail inside PostgreSQL's
    catalog (``pg_type_typname_nsp_index`` duplicate key) — an intermittent boot
    failure that looks like nothing else. One creator per table removes the
    race; the ORM classes for control's tables stay importable for queries but
    are never created here.
    """
    from src.workspace.models import Base

    owned = ("plan_apply_history", "knowledge_documents", "file_drafts")
    missing = [name for name in owned if name not in Base.metadata.tables]
    if missing:
        # A typo in ``owned`` would silently create nothing and surface much
        # later as "no such table" at first use; fail at startup instead.
        raise RuntimeError(
            f"runtime-owned tables missing from ORM metadata: {missing}"
        )

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Base.metadata.tables[name] for name in owned],
        )

    logger.info("database_tables_initialized", tables=list(owned))


async def close_db() -> None:
    await _db.close()
