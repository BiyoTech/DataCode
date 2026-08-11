#!/usr/bin/env bash
# One-click development environment setup.
# Installs all dependencies for backend and frontend services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Detect docker compose command
if docker compose version &>/dev/null; then
  DOCKER_COMPOSE="docker compose"
elif docker-compose version &>/dev/null; then
  DOCKER_COMPOSE="docker-compose"
else
  echo "ERROR: Neither 'docker compose' nor 'docker-compose' found. Please install Docker."
  exit 1
fi

echo "============================================"
echo "  AgenticData - Dev Environment Setup"
echo "============================================"
echo ""

# --- Infrastructure (optional) ---
echo "==> [1/6] Starting infrastructure (PostgreSQL, Redis, Gitea)..."
cd "$ROOT_DIR"
$DOCKER_COMPOSE -f deploy/docker-compose.infra.yml up -d
echo "    Infrastructure started."
echo ""

# --- Python services ---
echo "==> [2/7] Setting up SQLMesh Service (Python)..."
cd "$ROOT_DIR/sqlmesh-service"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e "$ROOT_DIR/shared/python"
pip install -q -e ".[dev]"
deactivate
echo "    SQLMesh Service ready."
echo ""

echo "==> [3/7] Setting up Agent Service (Python)..."
cd "$ROOT_DIR/agent-service"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e "$ROOT_DIR/shared/python"
pip install -q -e ".[dev]"
deactivate
echo "    Agent Service ready."
echo ""

echo "==> [4/7] Setting up Ontology Service (Python)..."
cd "$ROOT_DIR/ontology-service"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e ".[dev]"
deactivate
echo "    Ontology Service ready."
echo ""

echo "==> [5/7] Setting up Dagster Service (Python)..."
cd "$ROOT_DIR/dagster-service"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e "$ROOT_DIR/shared/python"
pip install -q -e ".[dev]"
deactivate
echo "    Dagster Service ready."
echo ""

# --- Go services ---
echo "==> [6/7] Setting up Gateway (Go)..."
cd "$ROOT_DIR/gateway"
go mod tidy 2>/dev/null || echo "    (go mod tidy skipped - missing deps will be resolved on first build)"
echo "    Gateway ready."
echo ""

# --- Frontend ---
echo "==> [7/7] Setting up Frontend (Vue 3)..."
cd "$ROOT_DIR/frontend"
npm install
echo "    Frontend ready."
echo ""

echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Next: run 'make dev' or './scripts/dev-start.sh'"
echo "  Default dev uses SQLite (no Docker required)."
echo "  For Docker infra: INFRA_MODE=docker make dev"
echo "============================================"
