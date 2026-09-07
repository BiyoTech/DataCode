"""Every dialect the UI offers must be loadable, or declared as opt-in.

SQLMesh validates a workspace's ``config.yaml`` by IMPORTING the engine library
named in it (``sqlmesh/core/config/connection.py::_get_engine_import_validator``)
— before any connection, and for operations that never connect at all. A missing
driver therefore does not surface as "cannot reach the warehouse": it surfaces as
a 422 ``Failed to import the '<engine>' engine library`` with a traceback in the
logs, on every gateway listing, plan and apply of that workspace.

That is what a workspace created by REUSING an existing repository hit: its
committed config declared ``type: postgres``, and the runtime had installed
``asyncpg`` (the driver for its OWN database) but not ``psycopg2`` (the driver
SQLMesh imports). Two drivers, two purposes, one of them overlooked.

Assertions here are about the DECLARED policy rather than the current
environment, except for the core engines: whether an optional extra happens to
be installed in this venv must not decide whether the test passes.

The frontend cannot import Python, so its dialect list is parsed from the source
— the same way the provider table is pinned against the credential dialog.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from src.sqlmesh.config_access import load_workspace_config
from src.sqlmesh.external_models_service import ExternalModelsService

REPO_ROOT = Path(__file__).resolve().parents[2]
CREATE_DIALOG = (
    REPO_ROOT / "frontend" / "src" / "components" / "layout" / "WorkspaceSelector.vue"
)
PYPROJECT = REPO_ROOT / "workspace-runtime" / "pyproject.toml"

# Engine library SQLMesh imports per connection type. Mirrors sqlmesh's own
# validator calls; kept here because the point is to assert what the platform
# DECLARES, not what a given sqlmesh version happens to check.
ENGINE_IMPORTS: dict[str, str] = {
    "duckdb": "duckdb",
    "postgres": "psycopg2",
    "mysql": "pymysql",
    "clickhouse": "clickhouse_connect",
    "spark": "pyspark",
    "trino": "trino",
    "mssql": "pyodbc",
}

# Drivers in the CORE dependencies: every install has them, so a workspace
# targeting these engines always loads.
CORE_ENGINES = ("duckdb", "postgres")

# Dialects available only with an optional extra. Mapping them to the extra name
# is what makes "the UI offers it" honest: there is a documented way to get it.
OPT_IN_ENGINES: dict[str, str] = {
    "mysql": "mysql",
    "clickhouse": "clickhouse",
    "spark": "spark",
    "trino": "trino",
    "mssql": "mssql",
}


def _offered_dialects() -> set[str]:
    """Parse the dialect dropdown options out of the create-workspace dialog."""
    source = CREATE_DIALOG.read_text(encoding="utf-8")
    block = re.search(r"const dialectOptions\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert block, "dialectOptions not found in WorkspaceSelector.vue"
    return set(re.findall(r"value:\s*'([^']+)'", block.group(1)))


def _pyproject() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


def _declared_extras() -> set[str]:
    """Names of the optional-dependency groups this project declares."""
    section = _pyproject().split("[project.optional-dependencies]", 1)[-1]
    return set(re.findall(r"^(\w+)\s*=", section, re.M))


# ---------------------------------------------------------------------------
# Core engines: the regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", CORE_ENGINES)
def test_core_engine_drivers_are_importable(dialect: str) -> None:
    """A workspace targeting a core engine must always be loadable.

    The regression this catches: PostgreSQL is the platform's own engine, yet the
    runtime carried only ``asyncpg`` — the driver for its OWN database — and not
    ``psycopg2``, the one SQLMesh imports. Every postgres workspace then failed
    to load, including one created by reusing a repository that already had a
    postgres config.
    """
    module = ENGINE_IMPORTS[dialect]
    assert importlib.util.find_spec(module) is not None, (
        f"{module} is missing: SQLMesh cannot load any {dialect} workspace project "
        f"(it belongs in the core dependencies, or {dialect!r} must become opt-in)"
    )


@pytest.mark.parametrize("dialect", CORE_ENGINES)
def test_core_engine_drivers_are_core_dependencies(dialect: str) -> None:
    """The driver must be declared, not merely installed as a transitive dep."""
    module = ENGINE_IMPORTS[dialect]
    distribution = "psycopg2-binary" if module == "psycopg2" else module
    core = _pyproject().split("[project.optional-dependencies]", 1)[0]
    assert distribution in core, (
        f"{distribution} is not a core dependency of workspace-runtime; a "
        "transitive install can disappear on any dependency bump"
    )


# ---------------------------------------------------------------------------
# Coverage: nothing the UI offers may be unaccounted for
# ---------------------------------------------------------------------------


def test_every_offered_dialect_is_declared() -> None:
    """A dialect the UI offers must be core, or have a documented extra.

    The failure this prevents is quiet: the workspace is created, the UI shows
    it, and only once something reads its config does a 422 with a Python
    traceback appear.
    """
    offered = _offered_dialects()
    assert offered, "no dialects parsed; the dialog's option list changed shape"

    undeclared = sorted(offered - set(CORE_ENGINES) - set(OPT_IN_ENGINES))
    assert not undeclared, (
        "offered by the UI with no way to serve it: "
        f"{undeclared} — add them to ENGINE_IMPORTS and to CORE_ENGINES or "
        "OPT_IN_ENGINES"
    )


@pytest.mark.parametrize("dialect", sorted(OPT_IN_ENGINES))
def test_every_opt_in_dialect_has_an_extra(dialect: str) -> None:
    """Opting in must be possible: the extra named for the dialect must exist."""
    extra = OPT_IN_ENGINES[dialect]
    assert extra in _declared_extras(), (
        f"the UI offers {dialect!r} and routes it to the {extra!r} extra, but "
        "workspace-runtime declares no such extra"
    )


def test_no_dialect_is_both_core_and_opt_in() -> None:
    assert not (set(CORE_ENGINES) & set(OPT_IN_ENGINES))


# ---------------------------------------------------------------------------
# Behaviour: reading a config must not need a reachable warehouse
# ---------------------------------------------------------------------------


def test_list_gateways_reports_dialect_without_connecting() -> None:
    """The endpoint that failed: gateway listing needs the driver, not the DB.

    Guards the docstring's promise from the other side. A config whose host is
    unreachable still lists, which is what lets a workspace be opened while its
    warehouse is down — but only if the engine library can be imported, which is
    precisely what a missing driver prevents.
    """
    config = """
gateways:
  postgres:
    connection:
      type: postgres
      host: unreachable.invalid
      port: 5432
      user: nobody
      password: none
      database: none
default_gateway: postgres

model_defaults:
  dialect: postgres
"""
    workspace = tempfile.mkdtemp(prefix="engine-driver-")
    try:
        # The layout SQLMesh expects inside a working copy.
        project = Path(workspace) / "sql-warehouse"
        (project / "models").mkdir(parents=True)
        (project / "config.yaml").write_text(config.lstrip(), encoding="utf-8")

        parsed = load_workspace_config(workspace)
        gateways = ExternalModelsService()._list_gateways_sync(parsed)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    assert gateways == [{"name": "postgres", "is_default": True, "dialect": "postgres"}]
