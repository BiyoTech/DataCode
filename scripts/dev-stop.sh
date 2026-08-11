#!/usr/bin/env bash
# Stop all development services and infrastructure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Detect docker compose command
if docker compose version &>/dev/null; then
  DOCKER_COMPOSE="docker compose"
elif docker-compose version &>/dev/null; then
  DOCKER_COMPOSE="docker-compose"
else
  echo "WARNING: Docker not found, skipping infrastructure shutdown."
  DOCKER_COMPOSE=""
fi

echo "==> Stopping application services..."

# Kill any running dev processes by port
for port in 5173 8000 8001 8002 8003 50053; do
  pid=$(lsof -ti ":$port" 2>/dev/null || true)
  if [ -n "$pid" ]; then
    echo "    Stopping process on port $port (PID: $pid)"
    kill "$pid" 2>/dev/null || true
  fi
done

echo "==> Stopping infrastructure..."
if [ -n "$DOCKER_COMPOSE" ]; then
  cd "$ROOT_DIR"
  $DOCKER_COMPOSE -f deploy/docker-compose.infra.yml down
fi

echo "==> All services stopped."
