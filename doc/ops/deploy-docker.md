# Docker deployment

Three compose topologies, one repository of compose files under `deploy/`.
All of them mount the repository's `conf/` at `/app/conf` and select a
profile with `DATACODE_ENV` — the same mechanism as host dev and Kubernetes.

| Stack | Command | What it is |
|---|---|---|
| Infrastructure | `make infra` | PostgreSQL + Redis (+ Gitea) only |
| Single-machine | `make up` | ALL eight services in containers |
| + Dagster | `make dagster-up` | Webserver + daemon + sync, stackable on the above |
| Lease verification | `make data-plane-up` | The P4a shape; for `scripts/verify/verify-lease-routing.py` |
| Global tier | `make global-up` | Identity + global-pg (P7) |
| Cell | `make cell-up` | A complete cell (P7; needs `global-up` first) |

## Single-machine all-container stack

The "one machine, real topology" deployment: every service in a container,
including the frontend, resolving peers by compose service name. It
exercises the same images, the same config mechanism and the same startup
ordering as production.

```bash
make up                               # bootstraps deploy/.env, builds the images;
                                     # DB_PASSWORD and GW_JWT_SECRET are injected
                                     # from .data/ truth files (see configuration.md)
open http://localhost:8080            # the frontend (nginx) — API proxied to the gateway
```

Startup order is enforced by healthchecks and `depends_on`:
`postgres/redis → migrate (one-shot) → identity → control/agent/runtime →
gateway → frontend`. The migrate service applies control-service's alembic
chain ONCE, before any deployment starts — replicas never race migrations.

Stop it: `make down`. Follow logs: `make logs`. List containers: `make ps`.

## Adding the Dagster orchestration plane

```bash
make dagster-up
open http://localhost:3000
```

webserver + daemon + workspace-sync, in the same compose project. The sync
polls the runtime replicas' `/internal/code-locations` every 15s, so
workspaces appear in the UI a few seconds after they register.

## The P7 topology: global tier + cells

Cells are the unit of blast-radius isolation: each owns its database, Redis
and services; the global tier owns identity alone.

```bash
make global-up                        # global-pg + identity-service (datacode-global network)
make cell-up                          # cell-0: its own pg/redis + all services, joined to the global network
```

A second cell on the same host shifts every published port — compose cannot
do arithmetic, so each port is an explicit variable:

```bash
cp deploy/.env.cell-1.example deploy/.env.cell-1
DATACODE_CELL_ID=cell-1 docker compose --env-file deploy/.env.cell-1 \
  -f deploy/docker-compose.cell.yml -p datacode-cell-1 up -d
```

Cell identity arrives via `DATACODE_CELL_ID`; the cell's services share
`conf/platform.cell.yaml` unchanged.

## Images

```bash
make images                 # tag: contents of VERSION + git short sha
IMAGE_TAG=x make images     # explicit tag
IMAGE_REGISTRY=registry.example.com/team make images
```

Eight images (gateway, identity, control, agent, runtime, task-runner,
egress-proxy, frontend), all built from the repository root as context. No
image is ever referenced as `:latest` in a deployment — a mutable tag makes
"what is running" unanswerable after the second deploy.

## Data and volumes

| Stack | Volumes |
|---|---|
| single-machine | `postgres_data`, `redis_data` (infra) + `workspaces_1`, `workspaces_2` (per runtime replica) |
| global | `global_pg_data` |
| cell | `cell_pg_data`, `cell_redis_data`, `cell_workspaces_1/2` |

The runtime replicas deliberately get SEPARATE volumes: the lease guarantees
one writer per working copy, and a shared volume would turn a lease violation
into silent Git corruption instead of a visible error.

Backups: `docker compose ... exec postgres pg_dump` for the databases; the
workspace volumes are Git working copies whose WIP is snapshotted to the bare
repos every ≤10 minutes, so `pg_dump` + the volume contents is a consistent
recovery point.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `GW_JWT_SECRET` error at up | only possible on a bare `docker compose` run — make always injects it from `.data/gateway_jwt_secret`; export it yourself or run `scripts/jwt-secret.sh ensure` first |
| Port 5432/6379 already in use | stop the conflicting service, or your own local postgres/redis is running — the stacks expect to own these ports |
| Gitea vs a Dagster webserver on port 3000/3001 | no longer possible: Gitea publishes host **3002** (container stays 3000, `ROOT_URL` follows). 3000 = containerized webserver, 3001 = the host dev runtime's |
| Migrate service fails | check `docker compose logs migrate`; the database must be reachable and the password injected from `.data/postgres_password` (via make) correct |
