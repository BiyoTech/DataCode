# Dagster Control Plane

Control-plane support for DataCode's Dagster deployment. Contains exactly one
thing: the **code location registration channel** that upstream Dagster cannot
provide by itself.

## The problem it solves

A Dagster Code Location is a gRPC server that loads user Python code. In DataCode
that code lives on a **dagster-service (data plane)** replica's filesystem, so the
location is always local to that replica. The **control plane** — webserver +
daemon + metadata DB — can only drive a location whose `host:port` it knows, and
that is stated in its `workspace.yaml`. Only the data plane knows what it runs.

## How it works

`workspace_sync` is the **single writer** of the control plane's workspace file:

1. polls each data plane replica's `GET /internal/code-locations`
2. merges the snapshots (resolving name collisions by revision)
3. atomically writes `workspace.yaml` — **only when the content changed**
4. asks the webserver to `reloadWorkspace`
5. acknowledges the loaded revisions back via `POST /internal/code-locations/heartbeat`

The daemon needs no step 4: it re-reads the file on its own 60s cycle. Only the
webserver requires the explicit mutation.

```
┌─ control plane ──────────────────────┐      ┌─ data plane ─────────┐
│  workspace-sync (this package)       │─────▶│  dagster-service #1  │
│    writes workspace.yaml ────────────┼──┐   │    /internal/*       │
│  dagster-webserver  ◀── reload       │  │   │  dagster-service #2  │
│  dagster-daemon     ◀── 60s refresh  │◀─┘   │    (grpc locations)  │
└──────────────────────────────────────┘      └──────────────────────┘
```

### Why not have each replica write the control plane's file

N replicas mutating one file needs a distributed lock, a volume shared across the
plane boundary, and a write-conflict story — for what is derived state. One writer
polling N publishers needs none of it, and a replica that dies simply stops being
advertised.

### Why not a `python_module` workspace target

`load_from: python_module` produces a `ManagedGrpcPythonEnvCodeLocationOrigin`:
the control plane spawns a subprocess to import that module and expects a
`Definitions` object back. It cannot express "these are remote gRPC endpoints",
and the user code it would need is on the data plane's disk. `grpc_server` is the
only workspace target that describes a code location the control plane does not
own the code of.

## Configuration (env-only)

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DATACODE_DATA_PLANE_URLS` | yes | — | Comma-separated dagster-service base URLs. Must address replicas **individually** — a load-balanced URL would only ever reveal one replica's locations. |
| `DATACODE_INTERNAL_TOKEN` | yes | — | Shared secret; must equal the data plane's `DAGSTER_SERVICE_INTERNAL_TOKEN`. |
| `DATACODE_DAGSTER_GRAPHQL_URL` | yes | — | Webserver GraphQL endpoint, for the reload. |
| `DATACODE_WORKSPACE_FILE` | no | `/opt/dagster/dagster_home/workspace.yaml` | File to render. Must be the one the webserver and daemon load. |
| `DATACODE_DAGSTER_GRAPHQL_TOKEN` | no | — | Bearer token, if the webserver is authenticated. |
| `DATACODE_SYNC_INTERVAL_SECONDS` | no | `15` | Poll interval. Keep below the daemon's 60s refresh so a new deployment lands on its next cycle. |
| `DATACODE_SYNC_REQUEST_TIMEOUT_SECONDS` | no | `10` | Per-request timeout. |
| `DATACODE_CONTROL_PLANE_ID` | no | `dagster-control-plane` | Identifier sent on heartbeats, for log correlation. |
| `DATACODE_LOG_LEVEL` | no | `INFO` | |

Misconfiguration **exits non-zero** rather than degrading into a syncer that
silently keeps the control plane's workspace empty.

## Instance storage (`dagster.yaml`)

`dagster.yaml` in this directory configures the DagsterInstance shared by the
control plane's **webserver and daemon**. Mount it at
`$DAGSTER_HOME/dagster.yaml`.

It pins PostgreSQL storage because the auto-provisioned SQLite default cannot
serve this deployment: the webserver and the daemon are separate processes
writing the same run/event storage, and control-plane replicas do not share a
filesystem — per-replica SQLite files would mean a run launched on one replica
is invisible to the next.

| Variable | Required | Purpose |
|----------|----------|---------|
| `DAGSTER_PG_HOST` | yes | PostgreSQL host |
| `DAGSTER_PG_PORT` | yes | PostgreSQL port |
| `DAGSTER_PG_DB` | yes | Database name. Must be **separate** from the platform's `datacode` database: Dagster owns and migrates its schema itself, and it must not interleave with the services' alembic chain. |
| `DAGSTER_PG_USER` | yes | PostgreSQL user |
| `DAGSTER_PG_PASSWORD` | yes | Secret; env-only |

Dagster resolves these at load time and **fails startup** when one is missing,
which is deliberate: a control plane that quietly fell back to SQLite would
look healthy while losing cross-replica run visibility.

## Running

```bash
pip install -e .
datacode-workspace-sync
```

Container build (context is the `datacode/` directory, since the shared package is
a local path dependency):

```bash
docker build -f dagster-control-plane/Dockerfile -t datacode/workspace-sync .
```

## Operating notes

- **Registration lag is observable on the data plane**: each dagster-service's
  `/health` exposes `pending_registrations` — alive locations the control plane has
  not confirmed at their current revision. A list that does not drain means this
  syncer is broken, and it is the only signal available before users report
  missing assets.
- **A duplicate location name would take the whole control-plane workspace down**
  (Dagster raises on load), so collisions are resolved here: highest revision
  wins, and the loser is logged and **not** acknowledged.
- **A replica must fail `failure_tolerance` (3) consecutive polls** before its
  locations are dropped. Reacting to the first timeout would unregister a whole
  replica's deployments over one slow response.

## Tests

```bash
pytest tests/
```
