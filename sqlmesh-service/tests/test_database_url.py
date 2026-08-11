"""Tests for build_database_url()."""

from pathlib import Path

import pytest
from agenticdata_common.database.url import build_database_url


def test_sqlite_url_resolves_relative_path(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = build_database_url(
        db_type="sqlite",
        sqlite_path=".data/test.db",
        project_root=tmp_path,
        sqlite_default=".data/sqlmesh.db",
    )
    assert url.startswith("sqlite+aiosqlite:///")
    assert str(tmp_path / ".data" / "test.db") in url


def test_postgresql_url_encodes_special_password(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = build_database_url(
        db_type="postgresql",
        host="db.example.com",
        port=5432,
        name="app",
        username="user@corp",
        password="p@ss#word",
    )
    assert url.startswith("postgresql+asyncpg://")
    assert "user%40corp" in url
    assert "p%40ss%23word" in url


def test_mysql_url_default_port(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = build_database_url(
        db_type="mysql",
        host="localhost",
        name="agenticdata",
        username="root",
        password="secret",
    )
    assert "mysql+aiomysql://root:secret@localhost:3306/agenticdata" in url
    assert "charset=utf8mb4" in url


def test_database_url_env_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://override/db")
    url = build_database_url(db_type="sqlite")
    assert url == "postgresql+asyncpg://override/db"


def test_unknown_type_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="Unknown database type"):
        build_database_url(db_type="oracle")
