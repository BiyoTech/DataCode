"""Dagster control plane support for DataCode.

The control plane is the shared DagsterInstance: webserver + daemon + metadata
database. It is deliberately NOT a DataCode service — it is upstream Dagster,
deployed as-is — so the only DataCode code that belongs here is what the control
plane cannot do by itself.

That is exactly one thing: discovering the code locations that live on the data
plane. See :mod:`datacode_dagster_control_plane.workspace_sync`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
