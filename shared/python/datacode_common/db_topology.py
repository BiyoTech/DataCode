"""Global vs Cell database topology — and the guard that keeps them apart.

Two tiers, and the distinction is not stylistic
----------------------------------------------
* **Global** — one instance for the whole platform. Holds identity: users,
  tenants, roles, and the ``tenant_placement`` table that says which cell a
  tenant lives in. Every cell reads it; losing it stops new logins everywhere.
* **Cell** — one instance per cell. Holds that cell's workspaces, environments,
  quotas and audit records. Losing it affects exactly one cell's tenants, which
  is the entire point of cells.

Why a guard is needed
---------------------
Both tiers resolve through one mechanism (``database.services.<name>`` over a
base config), and an override that is missing, misspelled or partially filled in
silently falls back to the base — i.e. to the CELL database. Nothing fails: the
identity tables get created in the cell database and the platform keeps working,
as a single cell, forever. The blast-radius property P7 exists to deliver would
be quietly absent, and the way that surfaces is a second cell suddenly sharing
the first one's users.

So a multi-cell deployment must DECLARE that it is one (``cells.expect_global_db``
or ``DATACODE_EXPECT_GLOBAL_DB``), and this module refuses to start when the
declaration and the resolved topology disagree.

Single-cell installs are unaffected: with one cell, one database is the correct
topology, and the check is a no-op unless the declaration is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from datacode_common.cell import cell_id

# Deployment declares "this install has a separate Global database". Env var so
# it can be set per environment without editing the shared platform file.
ENV_EXPECT_GLOBAL_DB = "DATACODE_EXPECT_GLOBAL_DB"

# The service key whose database is the Global tier. One entry, named here so
# the string is not repeated across the services that consult it.
GLOBAL_DB_SERVICE = "identity"


class DatabaseTopologyError(RuntimeError):
    """The resolved database topology contradicts what the deployment declared.

    Raised at startup, never at request time: a topology mistake is a deployment
    fact, and discovering it on the first cross-cell login would mean the tenants
    of two cells already share a database.
    """


@dataclass(frozen=True)
class DatabaseTarget:
    """A resolved database, reduced to what identity comparison needs.

    Deliberately not the URL: URLs differ in password, pool arguments and driver
    while pointing at the same server and schema, so comparing them would report
    a split that does not exist (or miss one that does).
    """

    backend: str
    host: str
    port: int
    name: str

    @classmethod
    def from_url(cls, url: str) -> "DatabaseTarget":
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").split("+", 1)[0]
        # SQLite has no host; its identity IS the file path, which arrives in the
        # path component. Treating it uniformly keeps dev installs comparable.
        if scheme.startswith("sqlite"):
            return cls(backend="sqlite", host="", port=0, name=parsed.path or "")
        return cls(
            backend=scheme,
            host=(parsed.hostname or ""),
            port=int(parsed.port or 0),
            name=(parsed.path or "").lstrip("/"),
        )

    def describe(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite:{self.name}"
        return f"{self.backend}://{self.host}:{self.port}/{self.name}"

    def same_as(self, other: "DatabaseTarget") -> bool:
        """Whether both point at the same server AND the same database."""
        return (
            self.backend == other.backend
            and self.host == other.host
            and self.port == other.port
            and self.name == other.name
        )


def expects_global_database(declared: Optional[bool] = None) -> bool:
    """Whether this deployment declares a separate Global database.

    Args:
        declared: Value from the configuration file, when the caller has one.
            The environment variable wins, so an operator can flip it for one
            environment without touching a file every cell shares.
    """
    raw = os.environ.get(ENV_EXPECT_GLOBAL_DB)
    if raw is not None and raw != "":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(declared)


def assert_database_topology(
    *,
    global_url: str,
    cell_url: str,
    declared_global: Optional[bool] = None,
    service: str = "",
) -> None:
    """Refuse to start when the topology contradicts the declaration.

    Args:
        global_url: URL the identity (Global) tier resolved to.
        cell_url: URL this cell's own tier resolved to.
        declared_global: ``cells.expect_global_db`` from configuration.
        service: Caller's name, for the error message.

    Raises:
        DatabaseTopologyError: A separate Global database was declared but both
            tiers resolved to the same database. The reverse (no declaration,
            one database) is the correct single-cell topology and passes.
    """
    if not expects_global_database(declared_global):
        return

    global_target = DatabaseTarget.from_url(global_url)
    cell_target = DatabaseTarget.from_url(cell_url)
    if not global_target.same_as(cell_target):
        return

    raise DatabaseTopologyError(
        f"{service or 'this service'} in cell {cell_id()!r} declares a separate "
        f"Global database ({ENV_EXPECT_GLOBAL_DB} / cells.expect_global_db) but "
        f"both tiers resolved to {cell_target.describe()}. Set "
        f"database.services.{GLOBAL_DB_SERVICE} in conf/platform.yaml to the "
        f"Global instance: identity tables in a cell database mean every cell "
        f"shares that cell's users, which is the blast radius cells exist to "
        f"bound."
    )


__all__ = [
    "ENV_EXPECT_GLOBAL_DB",
    "GLOBAL_DB_SERVICE",
    "DatabaseTarget",
    "DatabaseTopologyError",
    "assert_database_topology",
    "expects_global_database",
]
