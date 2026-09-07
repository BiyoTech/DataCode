.PHONY: help setup doctor dev dev-backend dev-frontend dev-logs stop db-upgrade \
        pg-set-password \
        infra infra-down up build down ps logs dagster-up dagster-down data-plane-up data-plane-down \
        cell-up global-up images \
        k8s-render k8s-diff k8s-apply k8s-delete k8s-migrate \
        lint test clean

# Default target
help:
	@echo "DataCode - AI Native DataOps Platform"
	@echo ""
	@echo "Local (host):"
	@echo "  make setup        One-time dev setup: deps, venvs, .env, storage"
	@echo "  make doctor       Inspect the effective config per profile; check invariants"
	@echo "  make dev          Start everything (frontend + backend; storage auto via Docker)"
	@echo "  make dev-backend  Backend services only"
	@echo "  make dev-frontend Frontend only (Vite)"
	@echo "  make dev-logs     Follow the dev service logs (.data/logs/)"
	@echo "  make stop         Stop dev services (Docker infra left to infra-down)"
	@echo "  make db-upgrade   Create control tables + apply alembic (src.db_migrate)"
	@echo "  make pg-set-password PASSWORD=...  Change the postgres password (ALTER USER + truth file)"
	@echo ""
	@echo "Docker:"
	@echo "  make infra        Start postgres + redis (+ gitea) only"
	@echo "  make infra-down   Stop the infra stack"
	@echo "  make up           Single-machine ALL-container stack (8 services)  -> :8080"
	@echo "                    (reuses existing images; run make build after code changes)"
	@echo "  make build        Rebuild the stack images"
	@echo "  make down         Stop it (containers + networks; volumes and images kept)"
	@echo "  make ps           List its containers"
	@echo "  make logs         Follow its logs"
	@echo "  make dagster-up   Dagster orchestration plane (stackable on up/cell) -> :3000"
	@echo "  make dagster-down Stop the Dagster plane"
	@echo "  make data-plane-up   Lease-verification stack (see scripts/verify/)"
	@echo "  make data-plane-down Stop it"
	@echo "  make global-up    P7 global tier (identity + global-pg)"
	@echo "  make cell-up      P7 cell stack (needs global-up first)"
	@echo "  make images       Build all 8 images with a versioned tag (VERSION + git sha)"
	@echo ""
	@echo "Kubernetes (kustomize; OVERLAY=global|cell|kind):"
	@echo "  make k8s-render      Render the overlay to stdout (no cluster needed)"
	@echo "  make k8s-diff        Diff the cluster against the overlay"
	@echo "  make k8s-apply       Apply the overlay"
	@echo "  make k8s-migrate     Run the migration Jobs and wait for them"
	@echo "  make k8s-delete      Delete the overlay's resources"
	@echo ""
	@echo "Quality:"
	@echo "  make lint         Linters for every service (go vet + ruff + vue-tsc)"
	@echo "  make test         Test suites for every service"
	@echo "  make clean        Caches and build artifacts"

# Detect docker compose command
DOCKER_COMPOSE := $(shell if docker compose version > /dev/null 2>&1; then echo "docker compose"; elif docker-compose version > /dev/null 2>&1; then echo "docker-compose"; else echo ""; fi)

# The single source of truth for the local PostgreSQL password lives in
# .data/postgres_password (created/adopted by scripts/dev-lib.sh::
# ensure_pg_password). PG_PW is LAZY (=, not :=) and expands at recipe time
# into a command prefix, so every compose invocation below receives the
# password from the file — never from a stale shell export or a drifted
# deploy/.env. Targets that CREATE containers run `pg-password.sh ensure`
# first; read-only targets (down/ps/logs) just prefix and tolerate a missing
# file (an empty DB_PASSWORD changes nothing they do).
PG_PW_FILE := .data/postgres_password
PG_PW = DB_PASSWORD="$$(cat $(PG_PW_FILE))"

# Same truth-file contract for the token-signing secret: .data/gateway_jwt_secret
# (created by scripts/jwt-secret.sh ensure; `make dev` seeds the same file) is
# ONE value for the host dev stack and every container stack — a token minted
# by one stays verifiable on the other. Lazy like PG_PW, so shell env wins over
# --env-file and a stale GW_JWT_SECRET in deploy/.env can never win (that slot
# is documentation now). Unlike PG_PW this is prefixed onto the READ-ONLY
# targets as well, and they run `jwt-secret.sh ensure` too: compose's required
# `${GW_JWT_SECRET:?}` interpolation rejects an EMPTY value just like a missing
# one, and the ensure is a single cheap -f test once the file exists.
JWT_SECRET_FILE := .data/gateway_jwt_secret
JWT_SECRET = GW_JWT_SECRET="$$(cat $(JWT_SECRET_FILE))"

# The k8s overlay to operate on (global | cell | kind)
OVERLAY ?= cell

# ── Local (host) ───────────────────────────────────────────────────────────

# One-click dev environment setup (install all deps)
setup:
	@bash scripts/dev-setup.sh

# Configuration doctor: print the effective config per profile and check
# invariants (no service needs to run; safe in CI and before a release).
doctor:
	@control-service/.venv/bin/python scripts/config-doctor.py $(if $(PROFILE),--profile $(PROFILE)) $(if $(SHOW),--show)

# Start all development services (frontend + backend)
dev:
	@bash scripts/dev-start.sh

# Start only backend services
dev-backend:
	@bash scripts/dev-backend.sh

# Start only frontend
dev-frontend:
	@bash scripts/dev-frontend.sh

# Follow the per-service dev logs (written by dev-start.sh under .data/logs)
dev-logs:
	@ls .data/logs/*.log >/dev/null 2>&1 && tail -n 40 -F .data/logs/*.log || echo "No logs in .data/logs/ yet - start with: make dev"

# Stop all services
stop:
	@bash scripts/dev-stop.sh

# Create the control-plane tables and apply control-service's alembic chain to
# head (the same entry point every deployment's migrate step runs). dev-start.sh
# does this automatically; the target exists for manual runs and pipelines.
#
# The profile defaults to dev, mirroring dev-start.sh: an explicit
# DATACODE_ENV in the environment still wins. Without it the loader would run
# on code defaults only (no profile is loaded) and dial an address no shipped
# profile declares.
db-upgrade:
	@bash scripts/pg-password.sh ensure
	@cd control-service && DATACODE_ENV="$${DATACODE_ENV:-dev}" DB_PASSWORD="$$(cat ../$(PG_PW_FILE))" \
		./.venv/bin/python -m src.db_migrate

# Change the local PostgreSQL password the ONLY supported way: ALTER USER on
# the running instance, then update the truth file — one step, so the volume
# and every client stay in agreement. (Editing .data/postgres_password by
# hand desynchronizes them: the file would claim a password the volume does
# not hold.)
pg-set-password:
	@bash scripts/pg-password.sh set "$(PASSWORD)"

# ── Docker: infrastructure ────────────────────────────────────────────────

# Start infrastructure services. Gitea is opted in explicitly: its host port
# 3000 belongs to dagster-webserver in the Dagster stacks. The truth file must
# exist first — the postgres container bind-mounts it as POSTGRES_PASSWORD_FILE.
infra:
	@bash scripts/pg-password.sh ensure
	$(PG_PW) $(DOCKER_COMPOSE) -f deploy/docker-compose.infra.yml --profile gitea up -d

infra-down:
	$(PG_PW) $(DOCKER_COMPOSE) -f deploy/docker-compose.infra.yml --profile gitea down

# ── Docker: single-machine all-container stack ────────────────────────────

# The deploy/.env target is bootstrapped from the committed example on first
# use; never regenerated afterwards so user edits survive.
deploy/.env:
	@cp deploy/.env.example deploy/.env
	@echo "==> Created deploy/.env from deploy/.env.example"
	@echo "    Secrets need no edits: DB_PASSWORD and GW_JWT_SECRET are injected"
	@echo "    from .data/ truth files (created on demand)."

# Single-machine, every service in a container, profile `docker`. Migrations
# run in the one-shot migrate service BEFORE the deployments start.
#
# Deliberately WITHOUT --build: compose v2 reuses an image that already exists
# locally and only builds the ones that are missing, so a down/up cycle costs
# seconds instead of a full rebuild. The trade-off is that `make up` alone will
# keep running a stale image after a code change — run `make build` first (or
# `make build up`) in that case.
up: deploy/.env
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml up -d

# Rebuild the stack images after a code/dependency change. `make up` picks the
# new images up on the next run and recreates only the affected containers.
build: deploy/.env
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml build

down: deploy/.env
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml down

ps: deploy/.env
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml ps

logs: deploy/.env
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml logs -f --tail=100

# ── Docker: Dagster orchestration plane (stackable) ───────────────────────

# Merged with infra + stack so postgres/redis and the runtime replicas
# resolve; starting the merged project is idempotent for running services.
dagster-up: deploy/.env
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml \
		-f deploy/docker-compose.dagster.yml up -d

dagster-down: deploy/.env
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env \
		-f deploy/docker-compose.infra.yml -f deploy/docker-compose.stack.yml \
		-f deploy/docker-compose.dagster.yml rm --stop --force dagster-webserver dagster-daemon workspace-sync

# ── Docker: lease-verification stack ──────────────────────────────────────

# The P4a transitional shape (shared bind mount + leases + redis env lock)
# that test_deploy_manifests.py pins and verify-lease-routing.py checks.
data-plane-up: deploy/.env
	@bash scripts/pg-password.sh ensure
	$(PG_PW) $(DOCKER_COMPOSE) --env-file deploy/.env -f deploy/docker-compose.infra.yml \
		-f deploy/docker-compose.data-plane.yml up -d --build

data-plane-down: deploy/.env
	$(PG_PW) $(DOCKER_COMPOSE) --env-file deploy/.env -f deploy/docker-compose.infra.yml \
		-f deploy/docker-compose.data-plane.yml rm --stop --force workspace-runtime-1 workspace-runtime-2

# ── Docker: the P7 cell topology ──────────────────────────────────────────

global-up: deploy/.env
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env -f deploy/docker-compose.global.yml up -d --build

cell-up: deploy/.env
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	DATACODE_CELL_ID=$${DATACODE_CELL_ID:-cell-0} \
		$(PG_PW) $(JWT_SECRET) $(DOCKER_COMPOSE) --env-file deploy/.env -f deploy/docker-compose.cell.yml \
		-p datacode-$${DATACODE_CELL_ID:-cell-0} up -d --build

# ── Images ────────────────────────────────────────────────────────────────

images:
	@bash scripts/build-images.sh $(if $(IMAGE_TAG),-t $(IMAGE_TAG))

# ── Kubernetes ────────────────────────────────────────────────────────────

# All k8s targets pass --load-restrictor LoadRestrictionsNone because the
# conf/ ConfigMap is generated from the repository's conf/ directory (the
# single source of truth), which sits outside deploy/k8s/base/ by design.

k8s-render:
	kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$(OVERLAY)

k8s-diff:
	kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$(OVERLAY) | kubectl diff -f - || true

k8s-apply:
	kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$(OVERLAY) | kubectl apply -f -

k8s-delete:
	kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$(OVERLAY) | kubectl delete -f - --ignore-not-found

# Run the migration Jobs (control alembic + identity SQL) and wait for both.
# Run this BEFORE k8s-apply when the image carries schema changes; the
# Deployments boot with AUTO_MIGRATE=0 and refuse to serve on a stale schema.
k8s-migrate:
	kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$(OVERLAY) | kubectl apply -f - --selector=batch.kubernetes.io/job-name || true
	@# apply is filtered above to avoid touching deployments; run jobs directly:
	@echo "==> waiting for control-migrate and identity-migrate"
	kubectl -n datacode wait --for=condition=complete job/control-migrate --timeout=300s || true
	kubectl -n datacode-global wait --for=condition=complete job/identity-migrate --timeout=300s || true

# ── Quality ───────────────────────────────────────────────────────────────

lint:
	@echo "Running linters..."
	cd shared/go && go vet ./...
	cd gateway && go vet ./...
	cd identity-service && go vet ./...
	cd agent-service && ruff check src/ tests/
	cd control-service && ruff check src/ tests/
	cd workspace-runtime && ruff check src/ tests/
	cd shared/python && ruff check datacode_common/ tests/
	@# The SPA's type errors are otherwise only reachable through the frontend
	@# IMAGE build (`npm run build` runs vue-tsc) — the slowest feedback loop in
	@# the repo. Type-check here so they surface next to every other linter.
	cd frontend && npm run type-check

test:
	@echo "Running tests..."
	cd shared/go && go test ./...
	cd gateway && go test ./...
	cd identity-service && go test ./...
	pip install -q -e shared/python
	cd shared/python && python -m pytest tests/ -q
	cd agent-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q
	cd control-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q
	cd workspace-runtime && pip install -q -e ".[dev]" && python -m pytest tests/ -q

clean:
	rm -rf .ruff_cache frontend/dist
	find . -type d -name __pycache__ -not -path "./.git/*" -not -path "*/node_modules/*" -not -path "*/.venv/*" -exec rm -rf {} +
	find . -type d -name .pytest_cache -not -path "./.git/*" -not -path "*/node_modules/*" -not -path "*/.venv/*" -exec rm -rf {} +
	@echo "Cleaned caches and build artifacts."
