# deploy/

Every deployment shape of the platform. Choose by WHERE it runs; all shapes
load the SAME `conf/` (mounted read-only) and differ only in profile
(`DATACODE_ENV`) and topology.

## Deployment stacks

| File | What it is | Entry |
|---|---|---|
| `docker-compose.infra.yml` | postgres + redis (+ gitea) only | `make infra` |
| `docker-compose.stack.yml` | Single machine, EVERYTHING in containers: one-shot `migrate` service first, two runtime replicas (the lease router is only exercised with >1), gateway, frontend `:8080` | `make up` |
| `docker-compose.dagster.yml` | Dagster orchestration plane (webserver + daemon + workspace-sync). Stacks on `stack.yml` ONLY — a cell's postgres has no `dagster` database | `make dagster-up` |
| `docker-compose.global.yml` | The global tier: identity + its database | `make global-up` |
| `docker-compose.cell.yml` | ONE cell of a multi-cell installation (own PG, own Redis, own edge gateway) | `make cell-up CELL=cell-0` |

## Verification stacks

Not deployment targets. They exist to make a specific property testable and
are used by `scripts/verify/`:

| File | What it verifies | Entry |
|---|---|---|
| `docker-compose.data-plane.yml` | The P4a lease shape (shared bind mount + leases + Redis env lock) against a host-run control plane; pinned by `test_deploy_manifests.py`, exercised by `verify-lease-routing.py` | `make data-plane-up` |

## Kubernetes

`k8s/base/` is a Kustomize base (all Deployments + headless runtime Service +
its execution-plane RBAC + Ingress + PDBs + `control-migrate`/`identity-migrate`
Jobs + a configMapGenerator that builds `datacode-conf` from `conf/` with
content hashes); `k8s/overlays/{cell,global,kind}` are thin and mostly DELETE
things their tier does not run. The egress plane (proxy + CoreDNS +
default-deny NetworkPolicy) ships in base and is removed by the global overlay.

```
make k8s-render OVERLAY=cell    # render + sanity-check, no cluster needed
make k8s-tag TAG=0.1.0-gabc123  # point the manifests at the images you built
make k8s-migrate OVERLAY=cell   # apply + wait for the migrate Jobs
```

Two things the base deliberately does NOT provide, because a real install has
them already:

- **PostgreSQL and Redis.** The profiles name `postgres.datacode.svc`,
  `redis.<ns>.svc` and `global-pg.datacode-global.svc` so they can be
  re-pointed at managed endpoints by editing one profile. For a LOCAL cluster
  apply `k8s/local/infra.yaml`, which provides exactly those four endpoints on
  `emptyDir` storage.
- **Secrets.** See `k8s/base/secrets.example.yaml` — it lists every
  `secretKeyRef` in the base, per namespace, including the two values that must
  be created twice with identical content (a Secret cannot cross namespaces).

## Also here

- `orchestrator/` — the remote Dagster control plane definition (see its own
  README).
- `init-db/` — postgres bootstrap. `init.sql` creates only what the services
  cannot create themselves: the `dagster` and `gitea` databases. Platform
  tables belong to alembic.
- `prometheus-adapter-config.yaml` — custom-metrics adapter config for
  lease-count-based autoscaling.

## Ordering contract

Migrations run as a FIRST-CLASS step (compose one-shot `migrate` service /
K8s Jobs) BEFORE any replica starts; replicas run with `AUTO_MIGRATE=0`. The
migrate step creates the control-plane tables (`control-service/src/db_migrate.py`)
— alembic's 0001 baseline is empty on purpose, so `alembic upgrade head` alone
would create nothing.

Every application service in every stack declares that dependency, and
`workspace-runtime/tests/test_deploy_contracts.py` fails the build when one
does not: a single service without it is not a partial guarantee, it is the
one replica that races the migration.

## Secrets contract

Four values, one truth file each under `.data/`, injected into every compose
invocation by `make`:

| Variable | Truth file | Entry point |
|---|---|---|
| `DB_PASSWORD` | `.data/postgres_password` | `scripts/pg-password.sh` |
| `GW_JWT_SECRET` | `.data/gateway_jwt_secret` | `scripts/jwt-secret.sh` |
| `DATACODE_INTERNAL_TOKEN` | `.data/runtime_internal_token` | `scripts/stack-secrets.sh` |
| `DATACODE_TASK_TOKEN_SECRET` | `.data/task_token_secret` | `scripts/stack-secrets.sh` |

The same files `make dev` uses, so a token minted by a host-run service is
accepted by a containerized one. This is why the compose files REQUIRE these
variables (`${VAR:?}`) rather than defaulting them: a literal published in
this repository that survives into a real deployment is an authentication
bypass for every internal surface.

Detailed runbooks: `doc/ops/deploy-host.md`, `deploy-docker.md`,
`deploy-k8s.md`.
