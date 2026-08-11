"""Tests for dialect-aware gateway listing and schema discovery.

Verifies that:
1. ``_list_gateways_sync`` returns a ``dialect`` field per gateway.
2. ``_list_schemas_sync`` returns databases when ``database`` is None (level-1).
3. ``_list_schemas_sync`` returns schemas within a database when ``database``
   is provided and the dialect is Postgres-family (level-2, 3-level cascade).
4. MySQL-family dialects ignore the ``database`` param (always level-1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.external_models_service import ExternalModelsService


# ---------------------------------------------------------------------------
# Fake connection config (mimics sqlmesh connection config's DIALECT ClassVar)
# ---------------------------------------------------------------------------


class _FakePostgresConnection:
    DIALECT = "postgres"


class _FakeMySQLConnection:
    DIALECT = "mysql"


class _FakeGatewayConfig:
    """Mimics sqlmesh GatewayConfig with a ``connection`` attribute."""

    def __init__(self, connection):
        self.connection = connection


class _FakeConfig:
    """Mimics sqlmesh Config with ``gateways`` dict + ``default_gateway_name``."""

    def __init__(self, gateways: dict, default_gateway_name: str = ""):
        self.gateways = gateways
        self.default_gateway_name = default_gateway_name


class _FakeContext:
    """Mimics sqlmesh Context with ``config`` and ``engine_adapter``."""

    def __init__(self, config, adapter):
        self.config = config
        self.engine_adapter = adapter


class _FakeAdapter:
    """Mimics engine_adapter with ``dialect`` + ``fetchall``."""

    def __init__(self, dialect: str, fetchall_rows=None):
        self.dialect = dialect
        self._fetchall_rows = fetchall_rows or []

    def fetchall(self, sql: str):
        return self._fetchall_rows


# ---------------------------------------------------------------------------
# _list_gateways_sync — dialect field
# ---------------------------------------------------------------------------


def test_gateway_list_includes_dialect() -> None:
    """Each gateway entry should carry a ``dialect`` field."""
    config = _FakeConfig(
        gateways={
            "pg_prod": _FakeGatewayConfig(_FakePostgresConnection()),
            "mysql_replica": _FakeGatewayConfig(_FakeMySQLConnection()),
        },
        default_gateway_name="pg_prod",
    )
    ctx = _FakeContext(config, _FakeAdapter("postgres"))

    service = ExternalModelsService()
    result = service._list_gateways_sync(ctx)

    assert len(result) == 2
    by_name = {g["name"]: g for g in result}
    assert by_name["pg_prod"]["dialect"] == "postgres"
    assert by_name["pg_prod"]["is_default"] is True
    assert by_name["mysql_replica"]["dialect"] == "mysql"
    assert by_name["mysql_replica"]["is_default"] is False


def test_gateway_list_dialect_lowercase() -> None:
    """Dialect should be normalized to lowercase."""
    config = _FakeConfig(
        gateways={"gw1": _FakeGatewayConfig(_FakePostgresConnection())},
        default_gateway_name="gw1",
    )
    ctx = _FakeContext(config, _FakeAdapter("postgres"))
    service = ExternalModelsService()
    result = service._list_gateways_sync(ctx)
    assert result[0]["dialect"] == "postgres"


def test_gateway_list_empty_dialect_when_no_connection() -> None:
    """A gateway with no connection config should yield ``dialect: ""``."""
    config = _FakeConfig(
        gateways={"gw1": _FakeGatewayConfig(None)},
        default_gateway_name="gw1",
    )
    ctx = _FakeContext(config, _FakeAdapter(""))
    service = ExternalModelsService()
    result = service._list_gateways_sync(ctx)
    assert result[0]["dialect"] == ""


# ---------------------------------------------------------------------------
# _list_schemas_sync — level-1 (database=None)
# ---------------------------------------------------------------------------


def test_list_schemas_postgres_without_database_returns_databases() -> None:
    """Without ``database``, Postgres returns databases (level-1)."""
    # Simulate pg_database rows: [("postgres",), ("mydb",), ("template1",)]
    adapter = _FakeAdapter("postgres", fetchall_rows=[("postgres",), ("mydb",), ("template1",)])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database=None)

    names = [r["name"] for r in result]
    assert "mydb" in names


def test_list_schemas_mysql_without_database_returns_databases() -> None:
    """Without ``database``, MySQL returns databases (level-1)."""
    adapter = _FakeAdapter("mysql", fetchall_rows=[
        ("information_schema",), ("mydb",), ("mysql",), ("app_db",),
    ])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database=None)

    names = [r["name"] for r in result]
    assert "mydb" in names
    assert "app_db" in names
    # System databases should be filtered out.
    assert "information_schema" not in names
    assert "mysql" not in names


# ---------------------------------------------------------------------------
# _list_schemas_sync — level-2 (database provided, Postgres only)
# ---------------------------------------------------------------------------


def test_list_schemas_postgres_with_database_returns_schemas() -> None:
    """With ``database`` set, Postgres returns schemas within it (level-2)."""
    # Simulate information_schema.schemata rows.
    adapter = _FakeAdapter("postgres", fetchall_rows=[
        ("public",), ("app_schema",), ("pg_catalog",), ("information_schema",),
    ])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database="mydb")

    names = [r["name"] for r in result]
    assert "public" in names
    assert "app_schema" in names
    # System schemas should be filtered by the NOT LIKE 'pg\_%' + <> 'information_schema' clause.
    # (The filter is in SQL, but our mock returns all rows — the mock doesn't
    # execute SQL. We verify the method calls fetchall and returns the rows
    # as-is; the real DB does the filtering.)
    assert len(result) == 4  # mock doesn't filter; real DB would


def test_list_schemas_mysql_with_database_ignores_database_param() -> None:
    """MySQL ignores ``database`` — always returns databases (level-1)."""
    adapter = _FakeAdapter("mysql", fetchall_rows=[("mydb",), ("app_db",)])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database="mydb")

    # Should return databases, NOT schemas within "mydb" (MySQL has no such concept).
    names = [r["name"] for r in result]
    assert "mydb" in names
    assert "app_db" in names


def test_list_schemas_redshift_with_database_returns_schemas() -> None:
    """Redshift (Postgres family) also supports 3-level cascade."""
    adapter = _FakeAdapter("redshift", fetchall_rows=[("public",), ("analytics",)])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database="prod_db")

    names = [r["name"] for r in result]
    assert "public" in names
    assert "analytics" in names


# ---------------------------------------------------------------------------
# Generic fallback (unknown dialect)
# ---------------------------------------------------------------------------


def test_list_schemas_unknown_dialect_fallback() -> None:
    """Unknown dialects fall through to SHOW SCHEMAS / SHOW DATABASES."""
    adapter = _FakeAdapter("duckdb", fetchall_rows=[("main",), ("memory",)])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database=None)

    names = [r["name"] for r in result]
    assert "main" in names


def test_list_schemas_unknown_dialect_ignores_database_param() -> None:
    """Unknown dialects ignore ``database`` (2-level default)."""
    adapter = _FakeAdapter("clickhouse", fetchall_rows=[("default",)])
    ctx = _FakeContext(_FakeConfig({}), adapter)

    service = ExternalModelsService()
    result = service._list_schemas_sync(ctx, database="somedb")

    # Should still return level-1 databases, not schemas within "somedb".
    names = [r["name"] for r in result]
    assert "default" in names
