"""Global/Cell database topology tests (design 06 P7).

The guard exists because the failure it prevents is SILENT: identity resolves
through ``database.services.identity`` over a base config, so a missing or
misspelled override falls back to the CELL database, nothing errors, and the
platform keeps working — as one cell, forever. The blast-radius property cells
exist to deliver would be quietly absent, and it surfaces only when a second cell
starts sharing the first one's users.

So a multi-cell deployment DECLARES the split, and these tests pin that the
declaration is enforced in the direction that matters and only in that direction.
"""

from __future__ import annotations

import pytest

from datacode_common.db_topology import (
    ENV_EXPECT_GLOBAL_DB,
    DatabaseTarget,
    DatabaseTopologyError,
    assert_database_topology,
    expects_global_database,
)

CELL_DB = "postgresql+asyncpg://u:p@cell-pg:5432/datacode"
GLOBAL_DB = "postgresql+asyncpg://u:p@global-pg:5432/identity"


class TestDatabaseTarget:
    def test_credentials_and_driver_are_not_part_of_identity(self):
        """URLs differ in password, driver and pool args while pointing at the
        same server: comparing them would report a split that does not exist."""
        a = DatabaseTarget.from_url("postgresql+asyncpg://u:p1@h:5432/db")
        b = DatabaseTarget.from_url("postgresql+psycopg://u:p2@h:5432/db")

        assert a.same_as(b)

    def test_a_different_database_on_one_server_is_a_different_target(self):
        a = DatabaseTarget.from_url("postgresql://u:p@h:5432/identity")
        b = DatabaseTarget.from_url("postgresql://u:p@h:5432/datacode")

        assert not a.same_as(b)

    def test_a_different_host_is_a_different_target(self):
        assert not DatabaseTarget.from_url(GLOBAL_DB).same_as(
            DatabaseTarget.from_url(CELL_DB)
        )

    def test_sqlite_is_identified_by_its_file(self):
        """Dev installs must be comparable too, and a SQLite file has no host."""
        a = DatabaseTarget.from_url("sqlite+aiosqlite:///.data/identity.db")
        b = DatabaseTarget.from_url("sqlite+aiosqlite:///.data/control.db")

        assert a.backend == "sqlite"
        assert not a.same_as(b)
        assert a.same_as(DatabaseTarget.from_url("sqlite:///.data/identity.db"))

    def test_describe_omits_credentials(self):
        described = DatabaseTarget.from_url(GLOBAL_DB).describe()

        assert "global-pg" in described
        assert "p@" not in described


class TestDeclaration:
    def test_the_environment_variable_wins(self, monkeypatch):
        """An operator must be able to flip one environment without editing a
        file every cell shares."""
        monkeypatch.setenv(ENV_EXPECT_GLOBAL_DB, "1")

        assert expects_global_database(declared=False) is True

    def test_falsey_values_are_honoured(self, monkeypatch):
        monkeypatch.setenv(ENV_EXPECT_GLOBAL_DB, "0")

        assert expects_global_database(declared=True) is False

    def test_an_empty_variable_falls_back_to_configuration(self, monkeypatch):
        """An empty value is "unset", not "false": a blank env var in a
        deployment template must not silently disable the guard."""
        monkeypatch.setenv(ENV_EXPECT_GLOBAL_DB, "")

        assert expects_global_database(declared=True) is True

    def test_configuration_is_used_when_unset(self, monkeypatch):
        monkeypatch.delenv(ENV_EXPECT_GLOBAL_DB, raising=False)

        assert expects_global_database(declared=True) is True
        assert expects_global_database(declared=False) is False


class TestGuard:
    def test_a_real_split_passes(self, monkeypatch):
        monkeypatch.delenv(ENV_EXPECT_GLOBAL_DB, raising=False)

        assert_database_topology(
            global_url=GLOBAL_DB,
            cell_url=CELL_DB,
            declared_global=True,
            service="control-service",
        )

    def test_a_declared_but_missing_split_is_refused(self, monkeypatch):
        """THE case: the override was forgotten, so identity tables would be
        created in this cell's database and every cell would share its users."""
        monkeypatch.delenv(ENV_EXPECT_GLOBAL_DB, raising=False)

        with pytest.raises(DatabaseTopologyError) as excinfo:
            assert_database_topology(
                global_url=CELL_DB,
                cell_url=CELL_DB,
                declared_global=True,
                service="control-service",
            )

        message = str(excinfo.value)
        assert "control-service" in message
        # Actionable: it must name the setting to fix, not just complain.
        assert "database.services.identity" in message

    def test_one_database_without_a_declaration_is_the_correct_single_cell_setup(
        self, monkeypatch
    ):
        """The reverse direction must NOT fail: with one cell, one database is
        right, and demanding a split would break every dev machine."""
        monkeypatch.delenv(ENV_EXPECT_GLOBAL_DB, raising=False)

        assert_database_topology(
            global_url=CELL_DB,
            cell_url=CELL_DB,
            declared_global=False,
            service="control-service",
        )

    def test_the_environment_variable_alone_enables_the_guard(self, monkeypatch):
        """A deployment can enforce the split without the config file saying so."""
        monkeypatch.setenv(ENV_EXPECT_GLOBAL_DB, "true")

        with pytest.raises(DatabaseTopologyError):
            assert_database_topology(
                global_url=CELL_DB, cell_url=CELL_DB, declared_global=False
            )

    def test_a_split_by_database_name_on_one_server_counts_as_split(
        self, monkeypatch
    ):
        """Two databases on one Postgres is a weaker deployment than two servers,
        but it IS a split: the tables are separate, which is what the guard is
        about. Refusing it would block a valid staging topology."""
        monkeypatch.delenv(ENV_EXPECT_GLOBAL_DB, raising=False)

        assert_database_topology(
            global_url="postgresql://u:p@one-pg:5432/identity",
            cell_url="postgresql://u:p@one-pg:5432/datacode",
            declared_global=True,
        )
