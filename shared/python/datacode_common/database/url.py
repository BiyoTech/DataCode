"""Build SQLAlchemy async database URLs."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus


def default_port(db_type: str) -> int | None:
    if db_type == "postgresql":
        return 5432
    if db_type == "mysql":
        return 3306
    return None


def build_database_url(
    *,
    db_type: str,
    sqlite_path: str | None = None,
    host: str = "localhost",
    port: int | None = None,
    name: str = "datacode",
    username: str = "",
    password: str = "",
    project_root: Path | None = None,
    sqlite_default: str = ".data/datacode.db",
) -> str:
    """Return SQLAlchemy async URL for sqlite | postgresql | mysql."""
    normalized = db_type.lower().strip()
    if env_url := os.getenv("DATABASE_URL"):
        return env_url

    if normalized == "sqlite":
        path = Path(sqlite_path or sqlite_default)
        if not path.is_absolute():
            root = project_root or Path.cwd()
            path = (root / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}"

    effective_port = port if port is not None else default_port(normalized)
    if effective_port is None:
        raise ValueError(f"Unknown database type: {db_type}")

    user = quote_plus(username)
    passwd = quote_plus(password)

    if normalized == "postgresql":
        return f"postgresql+asyncpg://{user}:{passwd}@{host}:{effective_port}/{name}"
    if normalized == "mysql":
        return (
            f"mysql+aiomysql://{user}:{passwd}"
            f"@{host}:{effective_port}/{name}?charset=utf8mb4"
        )

    raise ValueError(f"Unknown database type: {db_type}")
