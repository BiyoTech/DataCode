# deploy/

Every deployment shape of the platform. Choose by WHERE it runs; all shapes
load the SAME `conf/` (mounted read-only) and differ only in profile
(`DATACODE_ENV`) and topology.

## Docker Compose stacks

| File | What it is | Entry |
|---|---|---|
| `docker-compose.infra.yml` | postgres + redis (+ gitea) only | `make infra` |
| `docker-compose.stack.yml` | Single machine, EVERYTHING in containers: one-shot `migrate` service first, two runtime replicas (lease router is only exercised with >1), gateway, frontend `:8080` | `make up` |
| `docker-compose.dagster.yml` | The remote Dagster control plane (webserver/daemon/PG) | `make dagster-up` |
| `docker-compose.cell.yml` | ONE cell of a multi-cell installation (own PG, own edge gateway) | `make cell-up` |
| `docker-compose.global.yml` | The global tier: identity + global edge | `make global-up` |
| `docker-compose.data-plane.yml` | Extra workspace-runtime replicas against a host-run control plane | `make data-plane-up` |

## Kubernetes

`k8s/base/` is a Kustomize base (all Deployments + headless runtime Service +
Ingress + PDBs + `control-migrate`/`identity-migrate` Jobs + a
configMapGenerator that builds `datacode-conf` from `conf/` with content
hashes); `k8s/overlays/{cell,global,kind}` are thin and mostly DELETE things
their tier does not run. The egress plane (proxy + CoreDNS + default-deny
NetworkPolicy) ships in base and is removed by the global overlay.

```
make k8s-render OVERLAY=cell    # render + sanity-check
make k8s-apply  OVERLAY=cell
make k8s-migrate                # run the migrate Jobs before a rollout
```

## Also here

- `orchestrator/` — the remote Dagster control plane definition (see its own
  README).
- `init-db/` — postgres bootstrap (`init.sql` creates the dagster database
  only; platform tables belong to alembic).
- `prometheus-adapter-config.yaml` — custom-metrics adapter config for
  lease-count-based autoscaling.

## Ordering contract

Migrations run as a FIRST-CLASS step (compose one-shot `migrate` service /
K8s Jobs) BEFORE any replica starts; replicas run with `AUTO_MIGRATE=0`. The
migrate step creates the control-plane tables (`control-service/src/db_migrate.py`)
— alembic's 0001 baseline is empty on purpose, so `alembic upgrade head` alone
would create nothing.

Detailed runbooks: `doc/ops/deploy-host.md`, `deploy-docker.md`,
`deploy-k8s.md`.
