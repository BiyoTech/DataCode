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

## Running

The dagster-service launches this as a gRPC code location with:

```
dagster api grpc -f definitions.py --location-name <deployment>
```

with `cwd` set to this directory (so `import defs` resolves) and the deployment
identity injected as environment variables. The location name is the deployment
name, `{workspace_id}__{environment}`.
