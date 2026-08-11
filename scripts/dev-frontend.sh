#!/usr/bin/env bash
# Start only the frontend Vite dev server.
# Assumes backend services are already running (use dev-backend.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Starting Frontend (Vite dev server, port 5173)..."
echo "    Proxying /api -> http://localhost:8000"
echo "    Proxying /ws  -> ws://localhost:8000"
echo ""

cd "$ROOT_DIR/frontend"

# Install deps if node_modules missing
if [ ! -d "node_modules" ]; then
  echo "    Installing dependencies..."
  npm install
fi

exec npm run dev
