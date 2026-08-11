# asset-dag — Dagster code location for workspace {{ workspace_name }}

This is the Dagster project for your Data+AI pipeline. It follows the modern
Dagster (`dg`) layout: a single `definitions.py` entrypoint auto-discovers all
definitions under the `defs/` package.

## Layout

```
asset-dag/
├── definitions.py        # entrypoint: auto-discovers defs/ into Definitions
├── pyproject.toml        # project metadata; [tool.dagster] module_name
├── defs/                 # ALL definitions live here (auto-discovery root)
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
dagster api grpc -f definitions.py --location-name <name>
```

with `cwd` set to this directory, so `import defs` resolves.
