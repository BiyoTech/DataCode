#!/usr/bin/env bash
# Start only backend services in development mode (no frontend).
# Useful when developing frontend separately with `npm run dev`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Detect docker compose command
if docker compose version &>/dev/null; then
  DOCKER_COMPOSE="docker compose"
elif docker-compose version &>/dev/null; then
  DOCKER_COMPOSE="docker-compose"
else
  echo "ERROR: Neither 'docker compose' nor 'docker-compose' found."
  exit 1
fi

PIDS=()

cleanup() {
  echo ""
  echo "==> Stopping backend services..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null
  echo "==> Backend stopped."
  exit 0
}
trap cleanup SIGINT SIGTERM

echo "==> Checking infrastructure..."
if ! $DOCKER_COMPOSE -f "$ROOT_DIR/deploy/docker-compose.infra.yml" ps 2>/dev/null | grep -q "postgres"; then
  echo "    Starting infrastructure..."
  $DOCKER_COMPOSE -f "$ROOT_DIR/deploy/docker-compose.infra.yml" up -d
fi
echo "    Infrastructure OK."

# Gateway JWT secret is env-only. For local development we persist a random
# secret under .data/ (gitignored) so sessions survive restarts.
if [[ -z "${GW_JWT_SECRET:-}" ]]; then
  SECRET_FILE="$ROOT_DIR/.data/gateway_jwt_secret"
  if [[ ! -f "$SECRET_FILE" ]]; then
    mkdir -p "$(dirname "$SECRET_FILE")"
    openssl rand -base64 48 > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo "==> Generated a local development JWT secret at .data/gateway_jwt_secret"
  fi
  export GW_JWT_SECRET="$(cat "$SECRET_FILE")"
fi

echo "==> Starting SQLMesh Service (port 8002)..."
(
  cd "$ROOT_DIR/sqlmesh-service"
  source .venv/bin/activate
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8002 --reload 2>&1 | sed "s/^/[sqlmesh] /"
) &
PIDS+=($!)

echo "==> Starting Agent Service (port 8001)..."
(
  cd "$ROOT_DIR/agent-service"
  source .venv/bin/activate
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload 2>&1 | sed "s/^/[agent]  /"
) &
PIDS+=($!)

echo "==> Starting Ontology Service (port 8003)..."
(
  cd "$ROOT_DIR/ontology-service"
  source .venv/bin/activate
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8003 --reload 2>&1 | sed "s/^/[ontology] /"
) &
PIDS+=($!)

echo "==> Starting Dagster Service (port 8004)..."
(
  cd "$ROOT_DIR/dagster-service"
  source .venv/bin/activate
  python -m uvicorn src.main:app --host 0.0.0.0 --port 8004 --reload 2>&1 | sed "s/^/[dagster] /"
) &
PIDS+=($!)

echo "==> Starting Gateway (port 8000)..."
(
  cd "$ROOT_DIR/gateway"
  GW_JWT_SECRET="$GW_JWT_SECRET" go run ./cmd/server/ 2>&1 | sed "s/^/[gateway]/"
) &
PIDS+=($!)

echo ""
echo "============================================"
echo "  Backend services started!"
echo "============================================"
echo ""
echo "  Gateway API:       http://localhost:8000"
echo "  Agent Service:     http://localhost:8001"
echo "  SQLMesh Service:   http://localhost:8002"
echo "  Ontology Service:  http://localhost:8003"
echo "  Dagster Service:   http://localhost:8004"
echo ""
echo "  Frontend: run 'cd frontend && npm run dev' separately"
echo "  Press Ctrl+C to stop all backend services."
echo ""

wait
