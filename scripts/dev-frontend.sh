#!/usr/bin/env bash
# Start only the frontend Vite dev server, under the same lifecycle as the
# backend services: log file under .data/logs/, port cleanup, a readiness gate
# and a health check that the gateway (8000) is up first — a frontend that
# proxies to a dead gateway produces a stream of opaque network errors in the
# browser instead of one clear message here.
#
# Start the backend with dev-backend.sh first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

require_cmd node npm curl lsof

if ! check_port_open localhost 8000; then
  dev_die "the gateway is not listening on localhost:8000.
  The frontend proxies /api and /ws to it — start the backend first:
    make dev-backend"
fi

dev_info "proxying /api -> http://localhost:8000 and /ws -> ws://localhost:8000"

# Free the port from a previous run before starting.
kill_port_group 5173 "frontend (Vite)"

if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  dev_info "[frontend] npm install"
  (cd "$ROOT_DIR/frontend" && npm install)
fi

SVC_NAMES=()
SVC_PGIDS=()

cleanup() {
  local rc="${1:-0}"
  trap - INT TERM EXIT
  if [ "${#SVC_PGIDS[@]}" -gt 0 ]; then
    printf '\n'
    dev_info "stopping frontend"
    stop_svc_groups
  fi
  exit "$rc"
}
trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup "$?"' EXIT

start_svc frontend "$ROOT_DIR/frontend" npm run dev
wait_http "frontend" "http://localhost:5173/" 60 "$LOG_DIR/frontend.log"

dev_ok "frontend ready at http://localhost:5173 (Ctrl+C to stop)"

# Supervise: exit loudly if the dev server dies, instead of leaving a silent
# foreground process that succeeds at nothing.
while true; do
  sleep 2
  if ! kill -0 -- "-${SVC_PGIDS[0]}" 2>/dev/null; then
    dev_warn "frontend exited unexpectedly; last lines:"
    tail -n 30 "$LOG_DIR/frontend.log" 2>/dev/null | sed 's/^/    /' >&2
    exit 1
  fi
done
