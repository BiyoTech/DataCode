"""Async engine, session factory and session scope for the Python services.

The three services that talk to the platform database had a byte-identical copy
of this file, down to the log field names. Nothing here is service-specific: the
engine parameters come from configuration, and the session scope is the same
commit-or-rollback contract everywhere.

What is deliberately NOT here: ``init_db``. Which tables a service creates is the
one part that genuinely differs — the control plane creates the platform metadata
and the runtime creates only the working-copy tables it owns. Sharing that would
give each table a second creator, and concurrent ``CREATE TABLE IF NOT EXISTS``
can fail inside PostgreSQL's catalog (``pg_type_typname_nsp_index`` duplicate
key), an intermittent boot failure that looks like nothing else.

Each service holds its own instance of :class:`Database`, so its engine, pool and
lifecycle stay its own; only the code is shared.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from datacode_common.database.engine import create_async_engine_for_url
from datacode_common.database.url import safe_url
from datacode_common.logging import get_logger

logger = get_logger(__name__)

__all__ = ["Database"]


class Database:
    """Lazily-built engine and session factory for one service.

    The engine is created on first use rather than at import time: a service that
    never touches the database (a task runner, a test importing one module) must
    not open a pool, and configuration is not necessarily final at import.
    """

    def __init__(
        self,
        *,
        url_factory: Callable[[], str],
        db_type_factory: Callable[[], str],
        pool_size_factory: Callable[[], int],
        pool_overflow_factory: Callable[[], int],
    ) -> None:
        # Factories, not values: settings are read when the engine is actually
        # built, which is what lets tests point a service at a temporary database
        # without reaching into module globals.
        self._url_factory = url_factory
        self._db_type_factory = db_type_factory
        self._pool_size_factory = pool_size_factory
        self._pool_overflow_factory = pool_overflow_factory
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def get_engine(self) -> AsyncEngine:
        """The service's engine, created on first use."""
        if self._engine is None:
            url = self._url_factory()
            self._engine = create_async_engine_for_url(
                url,
                db_type=self._db_type_factory(),
                pool_size=self._pool_size_factory(),
                pool_overflow=self._pool_overflow_factory(),
            )
            logger.info(
                "database_engine_created",
                # The URL is logged (password masked) rather than the configured
                # host/port fields: DATABASE_URL can override them, and reporting
                # the config would name a server the service is not talking to.
                url=safe_url(url),
            )
        return self._engine

    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """The service's session factory, bound to its engine."""
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                bind=self.get_engine(),
                class_=AsyncSession,
                # Objects stay usable after commit: handlers routinely read the
                # row they just wrote to build a response, and expiring would
                # issue a second SELECT (or fail outside the session scope).
                expire_on_commit=False,
            )
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """A session that commits on success and rolls back on any exception."""
        factory = self.get_session_factory()
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        """Dispose the pool. Idempotent, so shutdown paths can call it freely."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database_connection_closed")

    def reset(self) -> None:
        """Drop the cached engine WITHOUT disposing it.

        For tests that repoint a service at a temporary database: disposing an
        engine built against the previous URL would need an event loop, which a
        synchronous fixture does not have.
        """
        self._engine = None
        self._session_factory = None
