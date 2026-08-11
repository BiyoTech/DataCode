"""Tests for build_database_url()."""

import pytest
from agenticdata_common.database.url import build_database_url


def test_mysql_uses_aiomysql_driver(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = build_database_url(
        db_type="mysql",
        host="localhost",
        port=3306,
        name="agenticdata",
        username="u",
        password="p",
    )
    assert url.startswith("mysql+aiomysql://")


def test_postgresql_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = build_database_url(
        db_type="postgresql",
        host="localhost",
        port=5432,
        name="db",
        username="u",
        password="pass/with/slash",
    )
    assert "pass%2Fwith%2Fslash" in url
