"""Daft DataFrame resource for Dagster assets/ops.

Provides a thin ``ConfigurableResource`` exposing Daft read/compute helpers to
user code inside a Dagster code location. Daft handles large-scale, distributed
DataFrame computation (feature engineering, transforms) that complements the
SQL-first SQLMesh warehouse.

Daft is imported lazily inside methods so that merely importing the definitions
module (e.g. during code location introspection) does not pay Daft's import
cost, and so environments without Daft installed can still load non-Daft assets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dagster import ConfigurableResource

if TYPE_CHECKING:  # avoid importing daft at module load
    import daft


class DaftResource(ConfigurableResource):
    """Expose Daft DataFrame operations to assets/ops.

    Attributes:
        num_partitions: Optional default partition hint for reads (0 = auto).
    """

    num_partitions: int = 0

    def _daft(self) -> "daft":
        import daft  # local import: see module docstring

        return daft

    def read_parquet(self, path: str, **kwargs: Any) -> "daft.DataFrame":
        """Read a Parquet dataset into a Daft DataFrame."""
        return self._daft().read_parquet(path, **kwargs)

    def read_csv(self, path: str, **kwargs: Any) -> "daft.DataFrame":
        """Read a CSV dataset into a Daft DataFrame."""
        return self._daft().read_csv(path, **kwargs)

    def from_pydict(self, data: dict[str, Any]) -> "daft.DataFrame":
        """Build a Daft DataFrame from an in-memory column dict."""
        return self._daft().from_pydict(data)

    def from_arrow(self, table: Any) -> "daft.DataFrame":
        """Build a Daft DataFrame from a PyArrow table."""
        return self._daft().from_arrow(table)


def make_daft_resource(num_partitions: int = 0) -> DaftResource:
    """Factory used by generated code location templates."""
    return DaftResource(num_partitions=num_partitions)
