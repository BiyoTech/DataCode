#!/usr/bin/env bash
# Start the full dev stack on the host: identity, control, agent, runtime,
# gateway — plus the Vite frontend unless --no-frontend.
#
# Dev storage contract (conf/platform.dev.yaml on top of conf/platform.yaml):
#   PostgreSQL  localhost:5432, database "datacode", user "datacode"
#   Redis       localhost:6379
# Both are provided by deploy/docker-compose.infra.yml and started AUTOMATICALLY
# when not already reachable (INFRA_MODE=none opts out). PostgreSQL everywhere —
# the same engine Docker and Kubernetes run — so a dialect defect surfaces on
# a laptop rather than in production.
#
# Migrations run once, BEFORE any service starts: control-service's alembic
# chain plus identity's boot-time runner (AUTO_MIGRATE=1 below). --skip-migrate
# skips that step for restarts against an already-migrated database.
#
# Secrets are env-only (never in conf/). Precedence: your shell environment
# wins over <repo>/.env (see .env.example); the three platform secrets are
# auto-generated under .data/ on first run and persist across restarts. The
# PostgreSQL password lives in ONE place — .data/postgres_password — created
# on first run (adopted from a running postgres or seeded), injected into
# everything that needs it, and changed only via `make pg-set-password`.
#
# Logs: .data/logs/<service>.log (truncated each run) plus live [name]-
# prefixed console output. Re-attach any time with `make dev-logs`.
#
# Stop: Ctrl+C — whole process groups die together (no orphaned go-run or
# uvicorn-reloader children), SIGKILL fallback after a 10s grace window.
# From another terminal: scripts/dev-stop.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

usage() {
  cat <<'EOF'
Usage: dev-start.sh [OPTIONS]

Options:
  --no-frontend   Start backend services only (same as dev-backend.sh)
  --skip-migrate  Skip the pre-start database migrations (restarts only)

Environment:
  DB_PASSWORD                 IGNORED — the truth is .data/postgres_password
                              (change it with: make pg-set-password PASSWORD=...)
  AGENT_SERVICE_LLM_API_KEY   LLM key for agent-service (without it every LLM
                              call fails at runtime; per-model overrides exist,
                              see conf/agent-service.yaml)
  INFRA_MODE=auto|none        auto (default): start postgres/redis via Docker
                              when not reachable; none: never start them

Loads <repo>/.env when present; the real environment always wins.
EOF
}

NO_FRONTEND=0
SKIP_MIGRATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-frontend) NO_FRONTEND=1 ;;
    --skip-migrate) SKIP_MIGRATE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 1 ;;
  esac
  shift
done

printf '\033[0;36m============================================\033[0m\n'
printf '\033[0;36m  DataCode - Development Mode\033[0m\n'
printf '\033[0;36m  PostgreSQL localhost:5432 + Redis localhost:6379\033[0m\n'
printf '\033[0;36m  profile: dev (conf/platform.dev.yaml)\033[0m\n'
printf '\033[0;36m============================================\033[0m\n'
printf '\n'

require_cmd go python3 node npm lsof nc openssl curl
load_dotenv "$ROOT_DIR/.env"

export DATACODE_ENV="${DATACODE_ENV:-dev}"

# --- PostgreSQL password: one truth file ------------------------------------
# .data/postgres_password is the single source of truth. First run creates it
# (adopting the password a running postgres already accepts, so an existing
# volume keeps working); afterwards the file wins over any DB_PASSWORD in the
# shell/.env. This also has to happen BEFORE ensure_infra, whose compose call
# bind-mounts the file into the postgres container.
ensure_pg_password

# --- Storage: reach it, or bring it up ------------------------------------
ensure_infra

# --- Platform secrets (persisted under .data/, gitignored) ---------------
# GW_JWT_SECRET: identity mints the tokens, the gateway verifies them
# locally — both sides must hold the same value. Same truth-file contract as
# the postgres password: once .data/gateway_jwt_secret exists it wins over
# any GW_JWT_SECRET in the shell, and the Makefile's container stacks inject
# the same file — tokens minted here verify there and vice versa.
ensure_jwt_secret
export GW_JWT_SECRET="$(cat "$JWT_SECRET_FILE")"
# identity internal token: guards identity-service's /internal/* API (the
# authz snapshots the gateway decides with + the binding writes it
# delegates, P5.5). Unset means the gateway denies every authorization-
# requiring route — fail closed by design — so dev generates and persists one.
ensure_secret IDENTITY_SERVICE_INTERNAL_TOKEN "$ROOT_DIR/.data/identity_internal_token" 32
# runtime internal token: guards workspace-runtime's /internal/* — the
# env-control command channel control-service dispatches every Git/SQLMesh side
# effect through, plus lease placement (asked by the gateway and by
# agent-service when a working copy is held by nobody yet). Fail closed when
# unset, so dev generates one and hands the SAME value to every party: the
# holder verifies it, and CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN is what control
# sends on that channel, which must match.
ensure_secret WORKSPACE_RUNTIME_INTERNAL_TOKEN "$ROOT_DIR/.data/runtime_internal_token" 32
export CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN="${CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN:-$WORKSPACE_RUNTIME_INTERNAL_TOKEN}"
# Task token secret (P6b): workspace-runtime mints sandbox tokens, the
# egress proxy verifies them. Deliberately distinct from GW_JWT_SECRET
# (users vs sandboxes). Dev does not start the egress proxy: there is no
# NetworkPolicy locally, so a configured proxy would only mediate SOME
# traffic — an illusion of control rather than the real thing.
ensure_secret DATACODE_TASK_TOKEN_SECRET "$ROOT_DIR/.data/task_token_secret" 32
# identity-service applies its own migrations in dev (production runs them
# as a separate, observable pipeline step).
export IDENTITY_SERVICE_AUTO_MIGRATE="${IDENTITY_SERVICE_AUTO_MIGRATE:-1}"

# --- Dependencies ---------------------------------------------------------
dev_info "checking dependencies"
ensure_go_deps gateway
ensure_go_deps identity-service
# Probes live in dev-lib.sh (ensure_service_venvs) — one definition shared
# with dev-setup.sh; the two entry points drifted once already and an older
# venv "passed" the stale probe without the PostgreSQL driver.
ensure_service_venvs
if [ "$NO_FRONTEND" -eq 0 ] && [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  dev_info "[frontend] npm install"
  (cd "$ROOT_DIR/frontend" && npm install)
fi

# --- Free stale ports from a previous (possibly crashed) run -------------
dev_info "freeing dev ports"
for port in $DEV_PORTS; do
  kill_port_group "$port" "$(dev_port_label "$port")"
done

# --- Migrations: one place, before any service starts --------------------
if [ "$SKIP_MIGRATE" -eq 0 ]; then
  run_db_migrations
else
  dev_info "skipping migrations (--skip-migrate)"
fi

# --- Lifecycle plumbing ---------------------------------------------------
SVC_NAMES=()
SVC_PGIDS=()

cleanup() {
  local rc="${1:-0}"
  trap - INT TERM EXIT
  if [ "${#SVC_PGIDS[@]}" -gt 0 ]; then
    printf '\n'
    dev_info "stopping all services"
    stop_svc_groups
    dev_ok "all services stopped"
  fi
  exit "$rc"
}
trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup "$?"' EXIT

# --- Start services -------------------------------------------------------
# identity first: it owns the identity tables, applies their migrations, and
# the gateway cannot authorize a single request without it (P5.5 — the
# gateway holds no identity data of its own any more).
start_svc identity-service "$ROOT_DIR/identity-service" go run ./cmd/server/
wait_http "identity-service" "http://localhost:8003/health" 90 "$LOG_DIR/identity-service.log"

# control, agent and runtime have no startup interdependency (only the
# gateway needs identity up); start them together and gate on all three.
start_svc control-service "$ROOT_DIR/control-service" \
  "$ROOT_DIR/control-service/.venv/bin/python" -m uvicorn src.main:app --host 0.0.0.0 --port 8004 --reload
start_svc agent-service "$ROOT_DIR/agent-service" \
  "$ROOT_DIR/agent-service/.venv/bin/python" -m uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload
# workspace-runtime owns the working copies: SQLMesh engine + ontology +
# Dagster data plane + git + files (design 06 P3). Heaviest import chain of
# the stack (sqlmesh + dagster), hence the generous timeout.
start_svc workspace-runtime "$ROOT_DIR/workspace-runtime" \
  "$ROOT_DIR/workspace-runtime/.venv/bin/python" -m uvicorn src.main:app --host 0.0.0.0 --port 8002 --reload

wait_http "control-service"   "http://localhost:8004/health" 120 "$LOG_DIR/control-service.log"
wait_http "agent-service"     "http://localhost:8001/health" 120 "$LOG_DIR/agent-service.log"
wait_http "workspace-runtime" "http://localhost:8002/health" 240 "$LOG_DIR/workspace-runtime.log"

start_svc gateway "$ROOT_DIR/gateway" go run ./cmd/server/
wait_http "gateway" "http://localhost:8000/health" 90 "$LOG_DIR/gateway.log"

if [ "$NO_FRONTEND" -eq 0 ]; then
  start_svc frontend "$ROOT_DIR/frontend" npm run dev
  wait_http "frontend" "http://localhost:5173/" 60 "$LOG_DIR/frontend.log"
fi

# --- Summary --------------------------------------------------------------
printf '\n'
dev_ok "all services started"
cat <<'EOF'

  Frontend:          http://localhost:5173
  Gateway API:       http://localhost:8000
  Identity Service:  http://localhost:8003   (identity / authz / placement)
  Agent Service:     http://localhost:8001
  Control Service:   http://localhost:8004   (registry / metadata / credentials)
  Workspace Runtime: http://localhost:8002   (working copies: sqlmesh + ontology + dagster)

  Storage:  PostgreSQL localhost:5432/datacode + Redis localhost:6379
  Profile:  dev (conf/platform.dev.yaml; inspect any profile: make doctor)
  Logs:     .data/logs/<service>.log   (re-attach: make dev-logs)

  Press Ctrl+C to stop all services.
EOF
printf '\n'

# --- Supervise ------------------------------------------------------------
# If any service dies, surface its log tail and stop the rest — a half-up
# stack fails in confusing ways otherwise.
dev_info "supervising (Ctrl+C to stop)"
while true; do
  sleep 2
  for i in "${!SVC_NAMES[@]}"; do
    if ! kill -0 -- "-${SVC_PGIDS[$i]}" 2>/dev/null; then
      dev_warn "${SVC_NAMES[$i]} exited unexpectedly; last lines:"
      tail -n 30 "$LOG_DIR/${SVC_NAMES[$i]}.log" 2>/dev/null | sed 's/^/    /' >&2
      exit 1   # EXIT trap -> cleanup() stops the remaining services
    fi
  done
done
