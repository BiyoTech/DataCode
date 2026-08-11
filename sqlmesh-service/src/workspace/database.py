"""Database connection and session management."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import AsyncGenerator

from agenticdata_common.database.engine import create_async_engine_for_url
from agenticdata_common.database.url import build_database_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = _build_database_url()
        _engine = create_async_engine_for_url(
            url,
            db_type=settings.db_type,
            pool_size=settings.db_pool_size,
            pool_overflow=settings.db_pool_overflow,
        )
        logger.info(
            "database_engine_created",
            db_type=settings.db_type,
            host=settings.db_host,
            port=settings.db_port,
            db=settings.db_name,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    if os.getenv("AUTO_MIGRATE", "").lower() in ("1", "true", "yes"):
        from alembic import command
        from alembic.config import Config

        ini_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        cfg = Config(str(ini_path))
        command.upgrade(cfg, "head")
        logger.info("database_migrated_via_alembic")
        return

    from src.workspace.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_tables_initialized")


async def close_db() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_connection_closed")
