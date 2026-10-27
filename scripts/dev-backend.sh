#!/usr/bin/env bash
# Start only the backend dev services (no Vite frontend).
# Thin wrapper around dev-start.sh --no-frontend: one implementation, two
# entry points, so backend-only and full-stack dev cannot drift apart.
#
# Useful when developing the frontend separately: cd frontend && npm run dev
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/dev-start.sh" --no-frontend "$@"
