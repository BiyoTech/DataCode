#!/usr/bin/env bash
# One-time dev environment setup: install every service's dependencies and
# bootstrap .env. Starts nothing (that is dev-start.sh's job).
#
# Dev storage is PostgreSQL + Redis, both provided by deploy/docker-compose.infra.yml.
# This script optionally brings them up (INFRA_MODE=auto, the default when
# Docker is present); dev-start.sh will also start them on demand.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

printf '\033[0;36m============================================\033[0m\n'
printf '\033[0;36m  DataCode - Dev Environment Setup\033[0m\n'
printf '\033[0;36m============================================\033[0m\n'
printf '\n'

dev_info "[1/6] checking toolchain"
require_cmd go python3 node npm lsof nc openssl curl
PY="$(find_python3)" || dev_die "no Python >= 3.11 found — install one (3.13 recommended)"
dev_ok "toolchain OK (python: $PY, node: $(node --version), go: $(go version 2>/dev/null | awk '{print $3}'))"

dev_info "[2/6] python services (per-service .venv)"
# Probes live in dev-lib.sh (ensure_service_venvs) — one definition shared
# with dev-start.sh.
ensure_service_venvs

dev_info "[3/6] go services"
ensure_go_deps gateway
ensure_go_deps identity-service

dev_info "[4/6] frontend"
if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  (cd "$ROOT_DIR/frontend" && npm install)
else
  dev_ok "node_modules present (rm -rf frontend/node_modules to force a reinstall)"
fi

dev_info "[5/6] .env"
if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  dev_ok "created .env from .env.example (AGENT_SERVICE_LLM_API_KEY is the only value worth setting there; the postgres password is managed in .data/postgres_password)"
else
  dev_ok ".env present"
fi

dev_info "[6/6] dev storage (PostgreSQL 5432 + Redis 6379)"
if [ "${INFRA_MODE:-auto}" != "none" ]; then
  # Bring the containers up (or reuse them when already reachable); a machine
  # with Docker therefore needs no manual storage setup at all.
  ensure_infra
elif check_port_open localhost 5432 && check_port_open localhost 6379; then
  dev_ok "PostgreSQL and Redis reachable"
else
  dev_warn "PostgreSQL (5432) or Redis (6379) not reachable — dev-start.sh will try Docker, or start your own"
fi

printf '\n'
dev_ok "setup complete"
cat <<'EOF'

  Next:
    1. make dev        (or: scripts/dev-start.sh) — one command, storage included
    2. make doctor     inspect the effective configuration (any profile)
    3. make dev-logs   re-attach to service logs (.data/logs/)
    4. make stop       stop everything

  PostgreSQL password: managed in .data/postgres_password (created on first
  run; change it with: make pg-set-password PASSWORD=...)

  Dev storage: PostgreSQL localhost:5432/datacode + Redis localhost:6379
  (started automatically via Docker when not already reachable).
EOF
