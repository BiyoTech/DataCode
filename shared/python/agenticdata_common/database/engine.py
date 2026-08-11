"""Async SQLAlchemy engine factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


def create_async_engine_for_url(
    url: str,
    *,
    db_type: str,
    pool_size: int = 5,
    pool_overflow: int = 10,
    echo: bool = False,
) -> AsyncEngine:
    """Create an async engine with dialect-appropriate pool settings."""
    normalized = db_type.lower()

    if normalized == "sqlite":
        return create_async_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
            echo=echo,
        )

    if normalized == "postgresql":
        return create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=pool_overflow,
            pool_timeout=5,
            pool_recycle=1800,
            echo=echo,
        )

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_overflow,
        pool_timeout=5,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5},
        echo=echo,
    )
