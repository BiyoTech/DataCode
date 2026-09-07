#!/usr/bin/env bash
# Stop dev services started by dev-start.sh / dev-backend.sh.
#
# Frees every dev port by terminating the WHOLE process group behind each
# listener (go run + compiled binary, uvicorn reloader + worker, npm + vite),
# waits up to 10s for a graceful exit, then SIGKILLs the survivors. Docker's
# port proxies are never signalled — a port owned by a container is reported
# with a hint instead (make stack-down / infra-down).
#
# Docker infrastructure started via INFRA_MODE=docker is deliberately NOT
# touched here: `make infra-down` owns it.
set -uo pipefail   # no -e: one failed kill must not abort the sweep

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

require_cmd lsof ps

dev_info "stopping dev services"
for port in $DEV_PORTS; do
  kill_port_group "$port" "$(dev_port_label "$port")"
done

# Grace window, then force-kill whatever of OURS still holds a dev port.
round=0
busy=""
while [ "$round" -lt 20 ]; do
  busy=""
  for port in $DEV_PORTS; do
    if [ -n "$(port_pids_ours "$port")" ]; then
      busy="yes"
    fi
  done
  [ -z "$busy" ] && break
  sleep 0.5
  round=$((round + 1))
done

if [ -n "$busy" ]; then
  dev_warn "grace period over — force killing survivors"
  for port in $DEV_PORTS; do
    kill_port_group "$port" "$(dev_port_label "$port")" KILL
  done
fi

dev_ok "dev services stopped"
dev_info "Docker infra (if running) is untouched; stop it with: make infra-down"
