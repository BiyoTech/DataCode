# asset-dag — Dagster code location for workspace {{ workspace_name }}

This is the Dagster project for your Data+AI pipeline. It follows the modern
Dagster (`dg`) layout: a single `definitions.py` entrypoint auto-discovers all
definitions under the `defs/` package.

This working copy belongs to ONE environment. Every environment of the workspace
runs its own Dagster deployment from its own Git branch, all loading this same
project — which is why the code must never assume it is running in production.

## Layout

```
asset-dag/
├── definitions.py        # entrypoint: auto-discovers defs/ into Definitions
├── pyproject.toml        # project metadata; [tool.dagster] module_name
├── defs/                 # ALL definitions live here (auto-discovery root)
│   ├── deployment.py     # MANAGED: deployment identity + schedule gate
│   ├── assets/           # asset definitions (SQLMesh bridge + Daft example)
│   ├── resources/        # resources, wired via @dg.definitions
│   ├── jobs/             # asset jobs
│   ├── schedules/        # schedules
│   └── sensors/          # sensors
└── tests/                # tests — sibling of defs/, NOT auto-discovered
```

## Conventions

- **Everything under `defs/` is auto-discovered.** Add a new module with a
  top-level `@asset`, job, schedule, or sensor and it shows up automatically.
- **Gate schedules and sensors on the deployment.** Use
  `default_status=DEFAULT_SCHEDULE_STATUS` (or `DEFAULT_SENSOR_STATUS`) from
  `defs.deployment`. Only a deployment the platform allows to be scheduled runs
  them; hard-coding `RUNNING` makes a branch drive production data on every tick.
- **`defs/deployment.py` is platform-managed.** It is regenerated whenever it
  drifts from the template, so edits there are lost. It also exposes the current
  `ENVIRONMENT`, `DEPLOYMENT_NAME` and `WORKSPACE_ID` for your own code.
- **Resources are wired explicitly.** Auto-discovery does not pick up bare
  resource objects; return them from a `@dg.definitions` function that yields a
  `Definitions(resources={...})` (see `defs/resources/example_resource.py`).
- **Use importable absolute imports** inside modules, e.g.
  `from defs.resources.example_resource import ...`. Never import the
  `asset-dag` directory (its hyphenated name is not a valid Python package).
- **Keep tests in `tests/`, not in `defs/`.** Modules under `defs/` are treated
  as definitions; test modules must stay out of the auto-discovery root.

## Adding a third-party package

Declare it in `pyproject.toml` under `[project].dependencies`, then press
**Reload** in the asset page (or run `dc deps sync`):

```toml
[project]
dependencies = [
    "polars>=1.0,<2",
]
```

The declaration is shared: it travels with this repository and this branch, and
the platform installs it once for the whole working copy — there is no per-user
environment. Reload shows the install as it happens and stops with the reason if
a package cannot be installed; it never starts the code location with the
packages missing, because your assets would then fail to import.

Two rules worth knowing:

- **`pip install` is refused.** A package installed by hand disappears with the
  sandbox that ran the command and nothing records why it was there.
- **Platform packages cannot be overridden.** Asking for a version of `dagster`,
  `sqlmesh` (or their own dependencies) that the platform does not ship fails the
  sync with the shipped version named, rather than installing a copy that would
  shadow the platform's and break the code location.

## Available packages

These come from the platform image, so they are importable with nothing declared
(declaring them anyway is fine — the sync recognises them and installs nothing):

| Module you import | Use |
| --- | --- |
| `dagster` | definitions: assets, jobs, schedules, sensors |
| `daft` | dataframe / multimodal computation (the example asset) |
| `pandas`, `numpy`, `pyarrow` | tabular work and columnar interchange |
| `sklearn`, `scipy` | classical ML and scientific computing |
| `sqlmesh`, `sqlglot` | the SQL bridge, for advanced use |
| `httpx` | HTTP calls |

The table lists import names: `sklearn` is how you import the `scikit-learn`
distribution, and `daft` how you import `getdaft`.

`dc deps status` reports what is declared, what is installed and when — the same
answer the asset page shows next to the Reload button.

Adding a package means adding it to the runtime image, not to this project: it
belongs in the `[usercode]` extra of `workspace-runtime/pyproject.toml`, which
is where the table above comes from and which a test keeps in sync with what is
actually installed.

## Running

The dagster-service launches this as a gRPC code location with:

```
dagster api grpc -f definitions.py --location-name <deployment>
```

with `cwd` set to this directory (so `import defs` resolves) and the deployment
identity injected as environment variables. The location name is the deployment
name, `{workspace_id}__{environment}`.
