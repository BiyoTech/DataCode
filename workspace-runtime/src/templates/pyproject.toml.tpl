# METADATA ONLY — nothing ever installs this file.
#
# Dagster reads [tool.dagster] module_name below to find the entrypoint, and
# that is its whole job. `dependencies` is documentation of what the code here
# was written against, NOT an install list: your assets run on the
# dagster-service's interpreter, which every code location on a replica shares,
# so what you can import is decided by that image. See "Available packages" in
# README.md — there is no per-workspace environment and no install step.

[project]
name = "asset-dag"
version = "0.1.0"
description = "Data+AI pipeline (Dagster + Daft) for workspace {{ workspace_name }}"
requires-python = ">=3.11"
dependencies = [
    "dagster>=1.8.0",
    "getdaft>=0.3.0",
    "httpx>=0.27.0",
]

[tool.dagster]
module_name = "definitions"
