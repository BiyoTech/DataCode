.PHONY: help setup doctor dev dev-backend dev-frontend dev-logs stop db-upgrade \
        pg-set-password ensure-secrets \
        infra infra-down up build down ps logs dagster-up dagster-down data-plane-up data-plane-down \
        cell-up cell-down global-up global-down images \
        k8s-render k8s-diff k8s-apply k8s-delete k8s-migrate k8s-tag \
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
	@echo "  make dagster-up   Dagster orchestration plane (stackable on up)     -> :3000"
	@echo "  make dagster-down Stop the Dagster plane"
	@echo "  make data-plane-up   Lease-verification stack (see scripts/verify/)"
	@echo "  make data-plane-down Stop it"
	@echo "  make global-up    P7 global tier (identity + global-pg)"
	@echo "  make global-down  Stop the global tier"
	@echo "  make cell-up      P7 cell stack (needs global-up first; CELL=cell-0)"
	@echo "  make cell-down    Stop one cell (CELL=cell-0)"
	@echo "  make images       Build all 8 images with a versioned tag (VERSION + git sha)"
	@echo ""
	@echo "Kubernetes (kustomize; OVERLAY=global|cell|kind):"
	@echo "  make k8s-render      Render the overlay to stdout (no cluster needed)"
	@echo "  make k8s-diff        Diff the cluster against the overlay"
	@echo "  make k8s-apply       Apply the overlay"
	@echo "  make k8s-migrate     Apply + run the migration Jobs and wait for them"
	@echo "  make k8s-tag TAG=... Point the manifests at an image tag (make images)"
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

# The same truth-file contract for the two remaining shared secrets. They are
# what let the compose files REQUIRE them (`${VAR:?}`) rather than default to a
# literal published in this repository: `${DATACODE_INTERNAL_TOKEN:-dev-internal-token}`
# is convenient locally and an authentication bypass for every /internal/*
# surface anywhere else. Prefixed onto read-only targets too — compose rejects
# a required variable that is empty exactly as it rejects a missing one.
INTERNAL_TOKEN_FILE := .data/runtime_internal_token
TASK_TOKEN_FILE := .data/task_token_secret
STACK_SECRETS = DATACODE_INTERNAL_TOKEN="$$(cat $(INTERNAL_TOKEN_FILE))" \
                DATACODE_TASK_TOKEN_SECRET="$$(cat $(TASK_TOKEN_FILE))"

# Every secret a container stack needs, ensured in one place so no target can
# ensure a subset (and then fail on the one it forgot).
COMPOSE_ENV = $(PG_PW) $(JWT_SECRET) $(STACK_SECRETS)
ensure-secrets:
	@bash scripts/pg-password.sh ensure
	@bash scripts/jwt-secret.sh ensure
	@bash scripts/stack-secrets.sh ensure

# The k8s overlay to operate on (global | cell | kind)
OVERLAY ?= cell

# Compose file sets, named once. The same `-f a -f b` string repeated across
# up/build/down/ps/logs is how one target ends up merging a different set than
# the others — and a `down` that merges fewer files than the `up` did leaves
# containers behind without saying so.
INFRA_FILE   := -f deploy/docker-compose.infra.yml
STACK_FILES  := $(INFRA_FILE) -f deploy/docker-compose.stack.yml
DAGSTER_FILES := $(STACK_FILES) -f deploy/docker-compose.dagster.yml
DATA_PLANE_FILES := $(INFRA_FILE) -f deploy/docker-compose.data-plane.yml

# Env files passed to compose, in order (compose applies them left to right;
# the LAST definition of a key wins). A second cell on one host overrides only
# its published ports:
#   make cell-up CELL=cell-1 ENV_FILES="deploy/.env deploy/.env.cell-1"
ENV_FILES ?= deploy/.env
ENV_FILE_ARGS = $(foreach f,$(ENV_FILES),--env-file $(f))

# Which cell the cell-* targets operate on. It is both the compose PROJECT name
# suffix (what actually separates two cells on one host) and the id every
# service in the stack reports, so it must not be guessed per invocation.
CELL ?= cell-0

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
	$(PG_PW) $(DOCKER_COMPOSE) $(INFRA_FILE) --profile gitea up -d

infra-down:
	$(PG_PW) $(DOCKER_COMPOSE) $(INFRA_FILE) --profile gitea down

# ── Docker: single-machine all-container stack ────────────────────────────

# The deploy/.env target is bootstrapped from the committed example on first
# use; never regenerated afterwards so user edits survive.
deploy/.env:
	@cp deploy/.env.example deploy/.env
	@echo "==> Created deploy/.env from deploy/.env.example"
	@echo "    Secrets need no edits: they are injected from .data/ truth"
	@echo "    files (created on demand)."

# Single-machine, every service in a container, profile `docker`. Migrations
# run in the one-shot migrate service BEFORE the deployments start.
#
# Deliberately WITHOUT --build: compose v2 reuses an image that already exists
# locally and only builds the ones that are missing, so a down/up cycle costs
# seconds instead of a full rebuild. The trade-off is that `make up` alone will
# keep running a stale image after a code change — run `make build` first (or
# `make build up`) in that case.
up: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(STACK_FILES) up -d

# Rebuild the stack images after a code/dependency change. `make up` picks the
# new images up on the next run and recreates only the affected containers.
build: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(STACK_FILES) build

down: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(STACK_FILES) down

ps: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(STACK_FILES) ps

logs: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(STACK_FILES) logs -f --tail=100

# ── Docker: Dagster orchestration plane (stackable) ───────────────────────

# Merged with infra + stack so postgres/redis and the runtime replicas
# resolve; starting the merged project is idempotent for running services.
# Only stackable on THIS stack: a cell's postgres has no `dagster` database
# (see the header of docker-compose.dagster.yml).
dagster-up: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(DAGSTER_FILES) up -d

dagster-down: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(DAGSTER_FILES) \
		rm --stop --force dagster-webserver dagster-daemon workspace-sync

# ── Docker: lease-verification stack ──────────────────────────────────────

# The P4a transitional shape (shared bind mount + leases + redis env lock)
# that test_deploy_manifests.py pins and verify-lease-routing.py checks.
data-plane-up: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(DATA_PLANE_FILES) up -d --build

data-plane-down: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) $(DATA_PLANE_FILES) \
		rm --stop --force workspace-runtime-1 workspace-runtime-2

# ── Docker: the P7 cell topology ──────────────────────────────────────────

# The global tier is a SINGLE stack for the whole installation, so it needs no
# CELL parameter; its compose project name comes from the file itself.
global-up: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) \
		-f deploy/docker-compose.global.yml up -d --build

# Brought down LAST: a cell's gateway verifies tokens this tier signs, and the
# cell compose file declares `datacode-global` as an EXTERNAL network, so
# removing this stack while a cell still runs leaves that cell unable to start.
global-down: deploy/.env ensure-secrets
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) \
		-f deploy/docker-compose.global.yml down

# One cell, selected by CELL (default cell-0). The compose PROJECT name is what
# separates two cells on one host, so it is derived from the same variable that
# every service in the stack reports as its id — they cannot drift apart.
cell-up: deploy/.env ensure-secrets
	DATACODE_CELL_ID=$(CELL) \
		$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) \
		-f deploy/docker-compose.cell.yml -p datacode-$(CELL) up -d --build

cell-down: deploy/.env ensure-secrets
	DATACODE_CELL_ID=$(CELL) \
		$(COMPOSE_ENV) $(DOCKER_COMPOSE) $(ENV_FILE_ARGS) \
		-f deploy/docker-compose.cell.yml -p datacode-$(CELL) down

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

# Apply the overlay and then WAIT for the migration Jobs. The previous
# implementation filtered the apply with `--selector=batch.kubernetes.io/
# job-name`, which selects nothing: that label is added by the Job controller
# to the PODS it creates, never to the Job object. The filtered apply created
# zero resources and the failing wait was swallowed by `|| true`, so the step
# reported success having done nothing.
#
# Applying everything is correct rather than merely convenient: the Deployments
# boot with AUTO_MIGRATE=0 and refuse to serve on a stale schema, so a replica
# that starts before its Job completes exits and is restarted — the Job is
# still the only migrator. The final rollout restart clears those backoffs at
# once instead of waiting out the CrashLoopBackOff timer.
#
# The waits are namespace-specific because the tiers live in different
# namespaces, and each is skipped silently when its overlay does not ship that
# Job (the cell overlay deletes identity-migrate; global deletes control-migrate).
k8s-migrate: k8s-apply
	@echo "==> waiting for the migration Jobs"
	@if kubectl -n datacode get job control-migrate >/dev/null 2>&1; then \
		kubectl -n datacode wait --for=condition=complete job/control-migrate --timeout=300s; \
	else echo "    control-migrate not present in overlay $(OVERLAY)"; fi
	@if kubectl -n datacode-global get job identity-migrate >/dev/null 2>&1; then \
		kubectl -n datacode-global wait --for=condition=complete job/identity-migrate --timeout=300s; \
	else echo "    identity-migrate not present in overlay $(OVERLAY)"; fi
	@echo "==> clearing any replica that crash-looped on the pre-migration schema"
	@kubectl -n datacode rollout restart deploy,statefulset 2>/dev/null || true
	@kubectl -n datacode-global rollout restart deploy 2>/dev/null || true

# Point every manifest at one image tag. `make images` builds with
# VERSION-g<sha> by default while the base kustomization pins `latest`, so
# without this the cluster silently keeps running whatever `latest` happens to
# be. Writes base/kustomization.yaml, which is committed on purpose: the tag a
# cluster runs is a reviewable fact, not a shell variable someone remembered.
#
# Rewritten in-place rather than with `kustomize edit set image`: that
# subcommand only exists in the standalone kustomize binary, and the rest of
# these targets deliberately need nothing but kubectl. Every `newTag:` in that
# file is an image tag, so the substitution needs no YAML parser.
k8s-tag:
	@test -n "$(TAG)" || { echo "usage: make k8s-tag TAG=<image tag>"; exit 1; }
	@python3 -c "import pathlib, re, sys; \
p = pathlib.Path('deploy/k8s/base/kustomization.yaml'); \
p.write_text(re.sub(r'(?m)^(\s*newTag:\s*).*$$', r'\g<1>' + sys.argv[1], p.read_text()))" "$(TAG)"
	@echo "==> deploy/k8s/base/kustomization.yaml now pins :$(TAG)"

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
