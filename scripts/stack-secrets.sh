#!/usr/bin/env bash
# stack-secrets.sh — executable entry point for the two truth files the
# CONTAINER stacks need beyond the postgres password and the JWT secret:
#
#   .data/runtime_internal_token   DATACODE_INTERNAL_TOKEN
#                                  guards every /internal/* surface (runtime's
#                                  env-control and lease channel, identity's
#                                  placement and snapshots)
#   .data/task_token_secret        DATACODE_TASK_TOKEN_SECRET
#                                  signs the sandbox egress tokens the
#                                  egress-proxy verifies
#
# The logic lives in dev-lib.sh (ensure_stack_secrets) so the dev scripts and
# this tool can never disagree; this wrapper exists only because dev-lib.sh is
# a sourced library rather than a runnable script. Both files are the SAME ones
# `make dev` uses, which is the point: a host-run service and a containerized
# one authenticate to each other without any further coordination.
#
# Used by the Makefile so every compose invocation receives the values, which
# is what lets the compose files require them (`${VAR:?}`) instead of carrying
# a well-known default — a literal like "dev-internal-token" that survives into
# a real deployment is an authentication bypass for every internal surface.
#
# Commands:
#   ensure   guarantee both truth files exist; print nothing
#   print    ensure, then print `VAR=value` lines (for eval / inspection)
#
# There is deliberately no `set`: rotating either secret has no live-instance
# coordination to perform (unlike the postgres password, which must be ALTERed
# on a running server). Delete the file and restart the stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

case "${1:-}" in
  ensure)
    ensure_stack_secrets
    ;;
  print)
    ensure_stack_secrets
    echo "DATACODE_INTERNAL_TOKEN=$(cat "$INTERNAL_TOKEN_FILE")"
    echo "DATACODE_TASK_TOKEN_SECRET=$(cat "$TASK_TOKEN_SECRET_FILE")"
    ;;
  *)
    dev_die "usage: stack-secrets.sh ensure | print"
    ;;
esac
