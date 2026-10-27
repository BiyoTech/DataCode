# Host development

Everything on your machine, one command, no container except (optionally)
the storage. This is the shape everyday development uses.

## Prerequisites

- Go 1.25+, Python 3.11+ (3.13 recommended; each service gets its own
  `.venv`), Node.js 20+
- Docker — only for PostgreSQL + Redis, and only if you don't run your own
  (`INFRA_MODE=none` opts out)

That's the whole list. Storage is PostgreSQL 5432 + Redis 6379, started
automatically via `deploy/docker-compose.infra.yml` when not already
reachable — the same engine Docker and Kubernetes run, so a dialect defect
surfaces on your laptop rather than in production.

## One-time setup

```bash
make setup             # toolchain check, per-service venvs, Go deps, npm install
```

`make setup` also brings the storage up (or reuses yours) and is safe to
re-run; it is idempotent throughout. The PostgreSQL password needs no setup:
it is created as `.data/postgres_password` on first use and managed from
there (see Secrets below).

## Daily loop

```bash
make dev          # start everything: storage → migrations → services → frontend
make dev-logs     # follow per-service logs (.data/logs/<service>.log)
make stop         # stop the services (Docker storage stays; make infra-down owns it)
```

`make dev` runs the full ordered startup:

1. **Storage** — PostgreSQL + Redis reachable, or started via Docker.
2. **Migrations** — control-service's alembic chain to head, then identity's
   boot-time migrations (idempotent; `--skip-migrate` for restarts).
3. **identity-service** (:8003) first — the gateway authorizes nothing
   without it.
4. **control / agent / runtime** (:8004 / :8001 / :8002) in parallel.
5. **gateway** (:8000), then **frontend** (Vite :5173).

Then it supervises: any service dying prints its log tail and stops the rest —
a half-up stack fails in confusing ways otherwise.

Backend only: `make dev-backend`. Frontend only: `make dev-frontend` (it
checks that the gateway is up first, since the Vite proxy targets it).

## Secrets

- `DB_PASSWORD` — not yours to set any more. The single truth is
  `.data/postgres_password`: created on first run (adopting the password a
  running postgres already accepts, so an existing volume keeps working),
  read by every flow, mounted into the postgres container as
  `POSTGRES_PASSWORD_FILE`. Change it only with
  `make pg-set-password PASSWORD=...` (ALTER USER + file, one step). A
  `DB_PASSWORD` in `.env`/the shell only seeds the file and is ignored
  afterwards.
- `GW_JWT_SECRET`, `IDENTITY_SERVICE_INTERNAL_TOKEN`,
  `DATACODE_TASK_TOKEN_SECRET` — auto-generated under `.data/` on first run,
  persisted, reused. Delete the files to rotate.
- `AGENT_SERVICE_LLM_API_KEY` — recommended in `.env`; without it every LLM
  call fails at runtime.

## Useful targets

| Command | What it does |
|---|---|
| `make doctor` | Inspect the effective config per profile; check invariants |
| `make doctor PROFILE=dev SHOW=1` | Full detail for the dev profile |
| `make db-upgrade` | Apply control-service's alembic migrations manually |
| `make lint` / `make test` | Lint / test every service |
| `make infra` / `make infra-down` | Start / stop the storage containers explicitly |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `postgres REJECTS the password in .data/postgres_password` | the volume was initialized with a different password; run `make pg-set-password PASSWORD=<the password the volume actually holds>` (keeps the data), or `docker compose -f deploy/docker-compose.infra.yml down -v` to start over (loses everything) |
| Storage unreachable and no Docker | start your own PostgreSQL + Redis, or install Docker |
| A service exits during startup | its log tail is printed; the full log is `.data/logs/<service>.log` |
| Port already in use (foreign process) | `make stop` only kills provably-ours; a squatter is reported with its pid — free the port manually |
| Identity data seems stale / needs reset | dev runs identity against the shared PostgreSQL database; drop/recreate the `datacode` database for a full reset |

## What this shape does not give you

No NetworkPolicy, so no sandbox egress plane — sandboxes get no proxy
configuration at all, which is the honest local state (see the capability
matrix in [00-README.md](00-README.md)). Task sandboxes run as hardened local
subprocesses (L3-proc). Working-copy leases are fully functional, since Redis
is real.
