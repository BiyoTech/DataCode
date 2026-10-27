#!/usr/bin/env bash
# pg-password.sh — executable entry point for the PostgreSQL password truth
# file (.data/postgres_password). The logic lives in dev-lib.sh so the dev
# scripts and this tool can never disagree; this wrapper only exists because
# dev-lib.sh is a sourced library, not a runnable script.
#
# Commands:
#   ensure            guarantee the truth file exists (adopt/seed — see
#                     ensure_pg_password in dev-lib.sh) and print nothing
#   print             ensure, then print the password (for piping into tools
#                     that want the value directly)
#   set <PASSWORD>    change the password the only supported way:
#                     ALTER USER on the live instance + update the file
#
# Used by the Makefile (infra / up / cell-up / ... and pg-set-password) so
# every compose invocation gets its DB_PASSWORD from the SAME file the dev
# scripts and the postgres containers use.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

case "${1:-}" in
  ensure)
    ensure_pg_password
    ;;
  print)
    ensure_pg_password
    printf '%s\n' "$DB_PASSWORD"
    ;;
  set)
    [ $# -eq 2 ] || dev_die "usage: pg-password.sh set <PASSWORD>"
    pg_set_password "$2"
    ;;
  *)
    dev_die "usage: pg-password.sh ensure | print | set <PASSWORD>"
    ;;
esac
