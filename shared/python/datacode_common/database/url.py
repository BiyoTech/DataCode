"""Build SQLAlchemy async database URLs.

The platform supports three backends — sqlite, mysql and postgresql — and the
backend is chosen at runtime from ``conf/platform.yaml``. This module is the
single place that maps a configured type onto a driver, so a service can never
end up on a different driver than its peers.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

# Canonical dialect names, with the spellings accepted in configuration.
# "postgres" is accepted alongside "postgresql" because both appear in the
# wild (and the Go side accepts both); silently rejecting one would surface as
# an unrelated "Unknown database type" much later than the typo.
_SQLITE = "sqlite"
_MYSQL = "mysql"
_POSTGRESQL = "postgresql"

_ALIASES = {
    "sqlite": _SQLITE,
    "sqlite3": _SQLITE,
    "mysql": _MYSQL,
    "mariadb": _MYSQL,
    "postgres": _POSTGRESQL,
    "postgresql": _POSTGRESQL,
}


def normalize_db_type(db_type: str) -> str | None:
    """Return the canonical dialect name, or ``None`` when unrecognized."""
    return _ALIASES.get(db_type.lower().strip())


def default_port(db_type: str) -> int | None:
    """Return the well-known port for a dialect (``None`` for sqlite)."""
    normalized = normalize_db_type(db_type)
    if normalized == _POSTGRESQL:
        return 5432
    if normalized == _MYSQL:
        return 3306
    return None


def safe_url(url: str) -> str:
    """Return *url* with any embedded password replaced by a placeholder.

    Connection URLs are routinely logged to record which backend a service came
    up on. They are also the one place a password appears in plain text, so
    masking has to happen here rather than at each call site — a single logger
    that forgets is a credential in the log store forever.
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return url

    credentials, at_sign, host_part = remainder.rpartition("@")
    if not at_sign:
        return url  # No credentials in this URL.

    user, colon, _password = credentials.partition(":")
    if not colon:
        return url  # Username only; nothing to mask.

    return f"{scheme}://{user}:***@{host_part}"


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
    """Return SQLAlchemy async URL for sqlite | postgresql | mysql.

    ``DATABASE_URL`` short-circuits everything else. That escape hatch is how a
    deployment expresses connection details this builder does not model (TLS
    parameters, connection proxies, alternate drivers).
    """
    if env_url := os.getenv("DATABASE_URL"):
        return env_url

    normalized = normalize_db_type(db_type)
    if normalized is None:
        raise ValueError(f"Unknown database type: {db_type}")

    if normalized == _SQLITE:
        path = Path(sqlite_path or sqlite_default)
        if not path.is_absolute():
            root = project_root or Path.cwd()
            path = (root / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}"

    effective_port = port if port is not None else default_port(normalized)
    user = quote_plus(username)
    passwd = quote_plus(password)

    if normalized == _POSTGRESQL:
        return f"postgresql+asyncpg://{user}:{passwd}@{host}:{effective_port}/{name}"

    return (
        f"mysql+aiomysql://{user}:{passwd}"
        f"@{host}:{effective_port}/{name}?charset=utf8mb4"
    )
