#!/usr/bin/env bash
# Start all services in development mode (zero external dependencies by default).
# Uses embedded DuckDB + SQLite + file locks — no Docker/MySQL/Redis/PostgreSQL needed.
# Ctrl+C stops everything.
#
# Usage:
#   ./dev-start.sh                       # Default venv in each service's .venv/
#   ./dev-start.sh --venv ~/.venvs/ad    # Use shared venv directory
#   INFRA_MODE=docker ./dev-start.sh     # Start external engines via Docker
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Parse arguments ---
VENV_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)
      VENV_DIR="$2"
      shift 2
      ;;
    --venv=*)
      VENV_DIR="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --venv PATH    Python virtual environment directory"
      echo "                 Default: each service uses its own .venv/"
      echo "                 If set, all Python services share one venv."
      echo ""
      echo "Environment variables:"
      echo "  INFRA_MODE     none (default) | docker"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Color output helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Track child PIDs for cleanup
PIDS=()

cleanup() {
  echo ""
  echo -e "${YELLOW}==> Stopping all services...${NC}"
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
    fi
  done
  wait 2>/dev/null
  echo -e "${GREEN}==> All services stopped.${NC}"
  exit 0
}
trap cleanup SIGINT SIGTERM

echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  AgenticData - Development Mode${NC}"
echo -e "${CYAN}  Zero external dependencies (DuckDB + SQLite)${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# --- Optional: external infrastructure ---
INFRA_MODE="${INFRA_MODE:-none}"
if [ "$INFRA_MODE" = "docker" ]; then
  echo -e "${BLUE}==> Starting external infrastructure via Docker...${NC}"
  if docker compose version &>/dev/null; then
    docker compose -f "$ROOT_DIR/deploy/docker-compose.infra.yml" up -d
  elif docker-compose version &>/dev/null; then
    docker-compose -f "$ROOT_DIR/deploy/docker-compose.infra.yml" up -d
  fi
  echo -e "${GREEN}    Docker infrastructure started.${NC}"
  echo ""
fi

# --- Ensure data directory exists (for SQLite & file locks) ---
mkdir -p "$ROOT_DIR/.data"

# --- Set AGENTICDATA_ENV for platform config (dev overrides) ---
export AGENTICDATA_ENV="${AGENTICDATA_ENV:-dev}"

# --- Database password (env-only) ---
# sqlmesh-service reads DB_PASSWORD from the environment. For local dev with
# the default MySQL, set this to your local root password. For zero-dep mode
# (SQLite), leave it empty — services that use SQLite ignore it.
if [ -z "${DB_PASSWORD:-}" ]; then
  export DB_PASSWORD="${DB_PASSWORD:-root@123}"
  echo -e "${YELLOW}==> DB_PASSWORD not set, using local dev default.${NC}"
fi

# --- Gateway JWT secret (env-only) ---
# For local development we persist a random secret under .data/ (gitignored)
# so issued sessions survive gateway restarts.
if [ -z "${GW_JWT_SECRET:-}" ]; then
  SECRET_FILE="$ROOT_DIR/.data/gateway_jwt_secret"
  if [ ! -f "$SECRET_FILE" ]; then
    openssl rand -base64 48 > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo -e "${BLUE}==> Generated local development JWT secret (.data/gateway_jwt_secret)${NC}"
  fi
  export GW_JWT_SECRET="$(cat "$SECRET_FILE")"
fi

# --- Ensure Go dependencies ---
echo -e "${BLUE}==> Checking Go dependencies...${NC}"
for svc in gateway; do
  if [ ! -f "$ROOT_DIR/$svc/go.sum" ]; then
    echo "    [$svc] Running go mod tidy..."
    (cd "$ROOT_DIR/$svc" && go mod tidy)
  fi
done
echo -e "${GREEN}    Go deps OK.${NC}"
echo ""

# --- Ensure Python venvs ---
echo -e "${BLUE}==> Checking Python dependencies...${NC}"
if [ -n "$VENV_DIR" ]; then
  # Shared venv mode: all Python services use one venv
  VENV_DIR="$(cd "$(dirname "$VENV_DIR")" 2>/dev/null && pwd)/$(basename "$VENV_DIR")" || VENV_DIR="$VENV_DIR"
  echo "    Using shared venv: $VENV_DIR"
  if [ ! -d "$VENV_DIR" ]; then
    echo "    Creating shared virtualenv..."
    python3 -m venv "$VENV_DIR"
  fi
  # Install deps for all Python services into the shared venv
  "$VENV_DIR/bin/pip" install -q -e "$ROOT_DIR/shared/python"
  for svc in sqlmesh-service agent-service ontology-service dagster-service; do
    if ! "$VENV_DIR/bin/python" -c "import fastapi, agenticdata_common" &>/dev/null; then
      echo "    [$svc] Installing dependencies into shared venv..."
      (cd "$ROOT_DIR/$svc" && "$VENV_DIR/bin/pip" install -q -e ".[dev]")
    fi
  done
else
  # Per-service venv mode (default)
  for svc in sqlmesh-service agent-service ontology-service dagster-service; do
    svc_dir="$ROOT_DIR/$svc"
    if [ ! -d "$svc_dir/.venv" ]; then
      echo "    [$svc] Creating virtualenv..."
      python3 -m venv "$svc_dir/.venv"
    fi
    needs_install=false
    if ! "$svc_dir/.venv/bin/python" -c "import fastapi" &>/dev/null; then
      needs_install=true
    fi
    if [[ "$svc" == sqlmesh-service || "$svc" == agent-service || "$svc" == dagster-service ]] && \
       ! "$svc_dir/.venv/bin/python" -c "import agenticdata_common" &>/dev/null; then
      needs_install=true
    fi
    if [ "$needs_install" = true ]; then
      echo "    [$svc] Installing dependencies..."
      if [[ "$svc" == sqlmesh-service || "$svc" == agent-service || "$svc" == dagster-service ]]; then
        "$svc_dir/.venv/bin/pip" install -q -e "$ROOT_DIR/shared/python"
      fi
      "$svc_dir/.venv/bin/pip" install -q -e "$svc_dir[dev]"
    fi
  done
fi
echo -e "${GREEN}    Python deps OK.${NC}"
echo ""

# --- Ensure Frontend node_modules ---
echo -e "${BLUE}==> Checking Frontend dependencies...${NC}"
if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  echo "    Installing npm packages..."
  (cd "$ROOT_DIR/frontend" && npm install)
fi
echo -e "${GREEN}    Frontend deps OK.${NC}"
echo ""

# --- Resolve venv activate path ---
get_venv_activate() {
  local svc="$1"
  if [ -n "$VENV_DIR" ]; then
    echo "$VENV_DIR/bin/activate"
  else
    echo "$ROOT_DIR/$svc/.venv/bin/activate"
  fi
}

# --- Kill stale processes on service ports ---
for port in 8001 8002 8003 8004; do
  pid=$(lsof -ti :$port 2>/dev/null || true)
  if [ -n "$pid" ]; then
    echo -e "${YELLOW}    Port $port is in use (PID: $pid), killing...${NC}"
    kill $pid 2>/dev/null || true
    sleep 1
  fi
done

# --- Start application services ---
echo -e "${BLUE}==> Starting SQLMesh Service (port 8002)...${NC}"
(
  cd "$ROOT_DIR/sqlmesh-service"
  source "$(get_venv_activate sqlmesh-service)"
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8002 --reload 2>&1 | sed "s/^/[sqlmesh] /"
) &
PIDS+=($!)

echo -e "${BLUE}==> Starting Agent Service (port 8001)...${NC}"
(
  cd "$ROOT_DIR/agent-service"
  source "$(get_venv_activate agent-service)"
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload 2>&1 | sed "s/^/[agent]  /"
) &
PIDS+=($!)

echo -e "${BLUE}==> Starting Ontology Service (port 8003)...${NC}"
(
  cd "$ROOT_DIR/ontology-service"
  source "$(get_venv_activate ontology-service)"
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8003 --reload 2>&1 | sed "s/^/[ontology] /"
) &
PIDS+=($!)

echo -e "${BLUE}==> Starting Dagster Service (port 8004)...${NC}"
(
  cd "$ROOT_DIR/dagster-service"
  source "$(get_venv_activate dagster-service)"
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8004 --reload 2>&1 | sed "s/^/[dagster] /"
) &
PIDS+=($!)

echo -e "${BLUE}==> Starting Gateway (port 8000)...${NC}"
(
  cd "$ROOT_DIR/gateway"
  go run ./cmd/server/ 2>&1 | sed "s/^/[gateway]/"
) &
PIDS+=($!)

echo -e "${BLUE}==> Starting Frontend (port 5173)...${NC}"
(
  cd "$ROOT_DIR/frontend"
  npx vite 2>&1 | sed "s/^/[frontend]/"
) &
PIDS+=($!)

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  All services started!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  ${CYAN}Frontend${NC}:          http://localhost:5173"
echo -e "  ${CYAN}Gateway API${NC}:       http://localhost:8000"
echo -e "  ${CYAN}Agent Service${NC}:     http://localhost:8001"
echo -e "  ${CYAN}SQLMesh Service${NC}:   http://localhost:8002 (includes workspace management)"
echo -e "  ${CYAN}Ontology Service${NC}:  http://localhost:8003 (semantic layer: entities/relationships/metrics)"
echo -e "  ${CYAN}Dagster Service${NC}:   http://localhost:8004 (Data+AI pipeline orchestration)"
echo ""
echo -e "  ${YELLOW}Storage${NC}: DuckDB (data engine) + SQLite (chat history) + file locks"
echo -e "  ${YELLOW}No external databases required!${NC}"
echo ""
echo -e "  Press ${RED}Ctrl+C${NC} to stop all services."
echo ""

# Wait for any child to exit
wait
