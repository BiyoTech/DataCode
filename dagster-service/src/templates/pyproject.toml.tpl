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
