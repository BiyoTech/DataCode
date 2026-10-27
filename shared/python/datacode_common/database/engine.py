"""Async SQLAlchemy engine factory."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from datacode_common.database.url import normalize_db_type

logger = logging.getLogger(__name__)


def dialect_from_url(url: str) -> Optional[str]:
    """Return the canonical dialect encoded in a SQLAlchemy URL.

    ``postgresql+asyncpg://...`` -> ``postgresql``.
    Returns ``None`` when the scheme is not one the platform supports.
    """
    scheme, separator, _ = url.partition("://")
    if not separator:
        return None
    return normalize_db_type(scheme.partition("+")[0])


def create_async_engine_for_url(
    url: str,
    *,
    db_type: str = "",
    pool_size: int = 5,
    pool_overflow: int = 10,
    echo: bool = False,
) -> AsyncEngine:
    """Create an async engine with dialect-appropriate pool settings.

    The dialect is taken from *url*, not from *db_type*. The two can legitimately
    disagree — ``DATABASE_URL`` overrides the configured backend — and the URL is
    what actually decides which driver runs. Trusting *db_type* here would hand
    the wrong connect arguments to that driver (for example MySQL's
    ``connect_timeout``, which asyncpg rejects outright). *db_type* is kept only
    as a fallback for callers that build a URL this module cannot classify.

    Pooling differs by backend rather than by preference:

    * **sqlite** serialises writers, so a pool buys nothing and turns contention
      into "database is locked". NullPool keeps it to one connection at a time.
    * **postgresql** rejects unknown connect args; its connect timeout belongs in
      the DSN rather than in ``connect_args``.
    * **mysql** takes ``connect_timeout`` so a dead server fails fast instead of
      hanging the request that opened the connection.
    """
    dialect = dialect_from_url(url) or normalize_db_type(db_type)
    if dialect is None:
        raise ValueError(f"Cannot determine database dialect from URL scheme: {url.partition('://')[0]}")

    configured = normalize_db_type(db_type) if db_type else None
    if configured and configured != dialect:
        # Nearly always a DATABASE_URL override; worth stating because it means
        # the service is not on the backend its config file describes.
        logger.info(
            "database_dialect_from_url dialect=%s configured=%s",
            dialect,
            configured,
        )

    if dialect == "sqlite":
        return create_async_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
            echo=echo,
        )

    common = {
        "pool_size": pool_size,
        "max_overflow": pool_overflow,
        "pool_timeout": 5,
        "pool_recycle": 1800,
        # Verify a pooled connection before handing it out: server-side idle
        # timeouts and connection proxies are common in both deployments, and
        # the failure would otherwise surface as a random request error.
        "pool_pre_ping": True,
        "echo": echo,
    }

    if dialect == "postgresql":
        return create_async_engine(url, **common)

    return create_async_engine(url, connect_args={"connect_timeout": 5}, **common)
