#!/usr/bin/env bash
# jwt-secret.sh — executable entry point for the GW_JWT_SECRET truth file
# (.data/gateway_jwt_secret). The logic lives in dev-lib.sh
# (ensure_jwt_secret) so the dev scripts and this tool can never disagree;
# this wrapper only exists because dev-lib.sh is a sourced library, not a
# runnable script.
#
# Commands:
#   ensure   guarantee the truth file exists (adopting an exported
#            GW_JWT_SECRET on first creation) and print nothing
#   print    ensure, then print the secret (for piping into tools that want
#            the value directly)
#
# Used by the Makefile (up / down / dagster-up / cell-up / ...) so every
# compose invocation gets GW_JWT_SECRET from the SAME file the dev scripts
# export — one value for the host stack and every container stack, which is
# what keeps a token minted in one verifiable by the others.
#
# There is deliberately no `set` subcommand (unlike pg-password.sh): rotating
# a signing secret has no live-instance coordination to perform — delete the
# file and every issued token dies with it, which IS the rotation semantics.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

case "${1:-}" in
  ensure)
    ensure_jwt_secret
    ;;
  print)
    ensure_jwt_secret
    cat "$JWT_SECRET_FILE"
    ;;
  *)
    dev_die "usage: jwt-secret.sh ensure | print"
    ;;
esac
