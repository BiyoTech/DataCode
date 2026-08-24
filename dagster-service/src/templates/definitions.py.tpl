"""Dagster Definitions entrypoint for workspace {{ workspace_name }}.

Loaded by the dagster-service Code Location gRPC server. This is the single
entrypoint: it auto-discovers every definition under the ``defs/`` package
(assets, jobs, schedules, sensors) and assembles them into one ``Definitions``.

Auto-discovery rules (Dagster ``dg`` convention):
- Top-level ``@asset`` / job / schedule / sensor objects in any ``defs/`` module
  are discovered automatically.
- Resources are NOT auto-discovered as bare objects; wire them by returning a
  ``Definitions(resources={...})`` from a ``@dg.definitions`` function (see
  ``defs/resources/example_resource.py``).

This code location serves ONE environment: dagster-service launches one per
environment, each loading that environment's own Git branch. The deployment
identity is injected as environment variables (``AGENTICDATA_ENVIRONMENT``,
``AGENTICDATA_DEPLOYMENT``, ``AGENTICDATA_DEPLOYMENT_TYPE``,
``AGENTICDATA_WORKSPACE_ID``, ``AGENTICDATA_SCHEDULES_ENABLED``) alongside the
SQLMesh bridge settings (``SQLMESH_SERVICE_URL``, ``DAGSTER_WORKSPACE_PATH``,
``SQLMESH_GATEWAY``); read it through ``defs/deployment.py``, which also gates
schedule and sensor statuses on it.
"""

import os

import dagster as dg

import defs as defs_root

# ``project_root`` must point at the directory holding pyproject.toml (this
# file's directory, i.e. asset-dag/). Passing it explicitly avoids Dagster
# walking parent directories to locate the project root.
defs = dg.load_defs(
    defs_root=defs_root,
    project_root=os.path.dirname(os.path.abspath(__file__)),
)
