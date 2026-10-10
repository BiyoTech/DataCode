#!/usr/bin/env bash
# dev-lib.sh — shared library for the dev scripts.
#
# Sourced (never executed) by dev-start.sh / dev-backend.sh / dev-stop.sh /
# dev-setup.sh. Owns everything the entry points have in common so they
# cannot drift apart again:
#   * repository / log directory resolution
#   * the single source of truth for dev ports (DEV_PORTS)
#   * .env loading (the real environment always wins)
#   * persisted secret generation (GW_JWT_SECRET and friends)
#   * the PostgreSQL password truth file (.data/postgres_password) — the
#     single source every flow reads the password from (dev scripts, make,
#     compose), so "what the clients use" and "what the volume holds" can
#     never drift apart the way a POSTGRES_PASSWORD env does
#   * per-service venv bootstrap with a Python >= 3.11 guard
#   * readiness gates (HTTP) and TCP reachability probes
#   * process-group-aware port cleanup: services started through
#     dev-svc-runner.py live in their own session, so signalling the GROUP
#     kills their whole tree (go run + compiled binary, uvicorn reloader +
#     worker, npm + vite) instead of orphaning the children.
#
# Conventions for callers:
#   ROOT_DIR / SCRIPT_DIR / LOG_DIR   resolved here
#   SVC_NAMES / SVC_PGIDS             filled by start_svc(); a runner's PID
#                                     doubles as its process-group id, so
#                                     `kill -- -PGID` stops the whole tree.

# ── Locations ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$ROOT_DIR/.data/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Single source of truth for the local PostgreSQL password. Established once
# (adopted from a running instance, or seeded), then read by EVERY flow that
# needs the password: dev scripts export it as DB_PASSWORD, Makefile injects
# it into compose invocations, and the postgres containers mount it as their
# POSTGRES_PASSWORD_FILE. A shell-level DB_PASSWORD that disagrees with this
# file is ignored with a warning — it no longer has the power to desynchronize
# the stack. See ensure_pg_password below.
PG_PASSWORD_FILE="$ROOT_DIR/.data/postgres_password"

# Every port the host dev stack may occupy — the single list used by
# dev-start.sh (stale-process cleanup) AND dev-stop.sh, so the two can never
# disagree about what "stopped" means.
#   8000 gateway            8001 agent-service (REST/WS)
#   8002 workspace-runtime  8003 identity-service
#   8004 control-service     5173 frontend (Vite)
#   50051 agent-service gRPC
#   3001  Dagster webserver (instance GraphQL); conf/workspace-runtime.yaml
#         dagster.webserver_port. Host 3000 belongs to the CONTAINERIZED
#         webserver (make dagster-up) and 3002 to Gitea, so all three can run
#         side by side. Not a service of its own: the
#         runtime spawns it as a child with start_new_session=True, so it sits
#         in its OWN process group — a group kill of the runtime does not reach
#         it. Listed here so a dev restart reclaims the port instead of leaving
#         a stale server that answers asset-DAG queries from a previous run.
DEV_PORTS="8000 8001 8002 8003 8004 5173 50051 3001"

# ── Output helpers ───────────────────────────────────────────────────────
dev_info() { printf '\033[0;34m==>\033[0m %s\n' "$*"; }
dev_ok()   { printf '\033[0;32m==>\033[0m %s\n' "$*"; }
dev_warn() { printf '\033[0;33m==>\033[0m %s\n' "$*" >&2; }
dev_die()  { printf '\033[0;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

dev_port_label() {
  case "$1" in
    8000) echo "gateway" ;;
    8001) echo "agent-service" ;;
    8002) echo "workspace-runtime" ;;
    8003) echo "identity-service" ;;
    8004) echo "control-service" ;;
    5173) echo "frontend (Vite)" ;;
    50051) echo "agent-service gRPC" ;;
    3001) echo "dagster webserver" ;;
    *) echo "dev service" ;;
  esac
}

# ── Generic helpers ──────────────────────────────────────────────────────

# require_cmd NAME... — fail loudly when a prerequisite binary is missing.
require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || dev_die "required command not found: $cmd"
  done
}

# detect_compose — set DOCKER_COMPOSE to the working compose command, or ""
# when Docker is unavailable. Never fails.
detect_compose() {
  DOCKER_COMPOSE=""
  if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
  fi
}

# find_python3 — print the name of the newest available Python >= 3.11.
# macOS still ships python3 == 3.9, which silently breaks venv creation for
# services declaring requires-python >= 3.11 (pip just skips every package,
# and the services then die with import errors that look like anything but a
# wrong interpreter).
find_python3() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

# load_dotenv FILE — export KEY=VALUE lines from FILE for keys that are NOT
# already present in the environment. The real environment always wins over
# the file, never the other way around. Handles simple single-line values,
# strips matching surrounding quotes; comments (#) and blank lines ignored.
load_dotenv() {
  local file="$1" line key value
  [ -f "$file" ] || return 0
  dev_info "loading environment from ${file#"$ROOT_DIR"/}"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < "$file"
}

# ensure_secret_file VAR FILE BYTES — guarantee the truth FILE exists, without
# exporting anything. On first creation an already-exported $VAR is adopted
# rather than replaced, so a value the caller already decided on wins exactly
# once and is then fixed. Idempotent and cheap (one -f test), which is what
# lets every Makefile target call it.
#
# Separate from ensure_secret below because the two consumers differ: host dev
# needs the variable in its environment, while the Makefile needs only the file
# (it reads it into each compose invocation). One value, two delivery paths.
ensure_secret_file() {
  local var="$1" file="$2" bytes="$3"
  if [ ! -f "$file" ]; then
    mkdir -p "$(dirname "$file")"
    if [ -n "${!var:-}" ]; then
      printf '%s\n' "${!var}" > "$file"
      dev_info "adopted $var from the environment into ${file#"$ROOT_DIR"/}"
    else
      openssl rand -base64 "$bytes" > "$file"
      dev_info "generated $var (${file#"$ROOT_DIR"/})"
    fi
    chmod 600 "$file"
  fi
}

# ensure_secret VAR FILE BYTES — guarantee $VAR holds a secret. When unset,
# generate one (openssl) and persist it under .data/ with 0600 so the value
# survives restarts; later runs reload instead of regenerating.
ensure_secret() {
  local var="$1" file="$2" bytes="$3"
  if [ -n "${!var:-}" ]; then
    return 0
  fi
  ensure_secret_file "$var" "$file" "$bytes"
  export "$var=$(cat "$file")"
}

# The truth files the CONTAINER stacks share with host dev. Same contract as
# the postgres password and the JWT secret: one file per secret, so a token
# minted by a host-run service is accepted by a containerized one and the
# compose files can require the variable (`:?`) instead of shipping a
# well-known default that would survive into a real deployment.
INTERNAL_TOKEN_FILE="$ROOT_DIR/.data/runtime_internal_token"
TASK_TOKEN_SECRET_FILE="$ROOT_DIR/.data/task_token_secret"

# ensure_stack_secrets — the two truth files every container stack needs.
ensure_stack_secrets() {
  ensure_secret_file DATACODE_INTERNAL_TOKEN "$INTERNAL_TOKEN_FILE" 32
  ensure_secret_file DATACODE_TASK_TOKEN_SECRET "$TASK_TOKEN_SECRET_FILE" 32
}

# ── PostgreSQL password: one truth file ───────────────────────────────────
#
# Why a file (and not an env var): POSTGRES_PASSWORD is a FIRST-INITIALIZATION
# parameter, not runtime config — the entrypoint skips initdb whenever the
# data volume already holds a PG_VERSION, so a changed DB_PASSWORD rebuilds
# containers with a new env that nothing ever reads, while every client keeps
# dialing with the new value. The password's "declaration" (what clients use)
# and its "fact" (what the volume holds) were two sources that could silently
# disagree. The truth file makes them one: postgres mounts it as
# POSTGRES_PASSWORD_FILE, every client reads it through DB_PASSWORD, and the
# ONLY way to change it is pg_set_password (ALTER USER + file, one step).

# pg_container_running — is OUR postgres container (deploy/docker-compose.infra.yml)
# up right now? False for stopped containers, foreign containers and setups
# without Docker — callers treat all three as "nothing to probe".
pg_container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' datacode-postgres 2>/dev/null)" = "true" ]
}

# pg_try_connect PASSWORD — succeed when PASSWORD authenticates against the
# running container. -h 127.0.0.1 forces TCP so pg_hba actually verifies the
# password (the container-internal unix socket is trust-authenticated and
# would accept anything).
pg_try_connect() {
  docker exec -e PGPASSWORD="$1" datacode-postgres \
    psql -h 127.0.0.1 -U datacode -d datacode -tAc 'SELECT 1' >/dev/null 2>&1
}

# write_pg_password VALUE — persist VALUE as the truth file, atomically, 0600.
write_pg_password() {
  local tmp="$PG_PASSWORD_FILE.tmp"
  mkdir -p "$(dirname "$PG_PASSWORD_FILE")"
  printf '%s' "$1" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$PG_PASSWORD_FILE"
}

read_pg_password() {
  cat "$PG_PASSWORD_FILE"
}

# ensure_pg_password — guarantee the truth file exists and DB_PASSWORD is
# exported from it. Idempotent.
#
# Bootstrap when the file does not exist yet:
#   * postgres container running -> probe candidates (the shell's DB_PASSWORD,
#     then the committed default) and ADOPT the one the instance accepts: the
#     volume's reality wins, existing setups keep working untouched;
#   * nothing running -> seed with the shell value or the default; a fresh
#     volume consumes it at first init, and a stopped volume that disagrees
#     is caught by verify_pg_password once it comes up.
#
# Once the file exists it always wins. A differing DB_PASSWORD in the shell
# (or in .env) is reported and ignored.
ensure_pg_password() {
  if [ -s "$PG_PASSWORD_FILE" ]; then
    local stored
    stored="$(read_pg_password)"
    if [ -n "${DB_PASSWORD:-}" ] && [ "$DB_PASSWORD" != "$stored" ]; then
      dev_warn "DB_PASSWORD from the environment/.env differs from ${PG_PASSWORD_FILE#"$ROOT_DIR"/} and is IGNORED — the file is the single source of truth. To change the password: make pg-set-password PASSWORD=..."
    fi
    export DB_PASSWORD="$stored"
    return 0
  fi

  local pw=""
  if pg_container_running; then
    local candidate
    for candidate in "${DB_PASSWORD:-}" datacode_dev; do
      [ -n "$candidate" ] || continue
      if pg_try_connect "$candidate"; then
        pw="$candidate"
        break
      fi
    done
    [ -n "$pw" ] || dev_die "the running postgres accepts neither the shell's DB_PASSWORD nor the committed default — its volume holds a third value. Align once with:

  make pg-set-password PASSWORD=<the password the volume actually holds>"
    dev_info "adopted the running postgres's password into ${PG_PASSWORD_FILE#"$ROOT_DIR"/} — the single source of truth from now on"
  else
    pw="${DB_PASSWORD:-datacode_dev}"
    dev_info "seeded ${PG_PASSWORD_FILE#"$ROOT_DIR"/} (no running postgres to consult; a fresh volume uses this at first init)"
  fi
  write_pg_password "$pw"
  export DB_PASSWORD="$pw"
}

# verify_pg_password — fail loudly when a running CONTAINERIZED postgres
# rejects the truth file (its volume was initialized with a different value;
# POSTGRES_PASSWORD_FILE only applies on first init). Non-container postgres
# cannot be probed here (no docker exec target) and is left to fail at
# connect time instead.
verify_pg_password() {
  pg_container_running || return 0
  if pg_try_connect "$(read_pg_password)"; then
    dev_ok "postgres accepts the password from ${PG_PASSWORD_FILE#"$ROOT_DIR"/}"
  else
    dev_die "postgres REJECTS the password in ${PG_PASSWORD_FILE#"$ROOT_DIR"/}.

The data volume was initialized with a different password: POSTGRES_PASSWORD*
only takes effect on the volume's FIRST initialization, so changing it later
never reaches the running instance. Keep the data and align (recommended):

  make pg-set-password PASSWORD=<the password the volume actually holds>

or destroy the volume and start fresh (loses ALL data):

  docker compose -f deploy/docker-compose.infra.yml down -v && make infra"
  fi
}

# pg_set_password NEW — change the postgres password the ONLY supported way:
# ALTER USER on the live instance, then update the truth file. ALTER runs
# first so a failure leaves nothing changed. Idempotent (same value twice is
# a no-op on both sides).
pg_set_password() {
  local new="$1"
  [ -n "$new" ] || dev_die "pg_set_password: empty password"
  if ! pg_container_running; then
    dev_die "cannot change the password: the datacode-postgres container is not running.
Start it first (make infra) — ALTER USER needs a live instance."
  fi
  # Single quotes are the only character SQL needs escaped in a string literal.
  local escaped="${new//\'/\'\'}"
  docker exec datacode-postgres psql -U datacode -d datacode -v ON_ERROR_STOP=1 \
    -c "ALTER USER datacode WITH PASSWORD '${escaped}'" \
    || dev_die "ALTER USER failed — the truth file was NOT touched, nothing changed"
  write_pg_password "$new"
  dev_ok "postgres password changed and ${PG_PASSWORD_FILE#"$ROOT_DIR"/} updated"
}

# ── JWT signing secret: one truth file ─────────────────────────────────────
#
# Same contract as the postgres password, minus the first-init pitfall:
# identity-service (mints) and every gateway (verifies) must hold ONE value
# across the host dev stack AND every container stack, or a token minted in
# one place is garbage in another. The truth file is that one value:
# dev-start.sh exports it, and the Makefile injects it into every compose
# invocation (scripts/jwt-secret.sh is the executable entry point). Once the
# file exists it wins over any GW_JWT_SECRET in the shell — a second source
# would silently fork the installation into mutually distrusting halves.
#
# Rotation has no live-instance coordination (unlike the pg password): delete
# the file and every issued token dies with it, which IS the rotation
# semantics. Hence no set subcommand in jwt-secret.sh.
JWT_SECRET_FILE="$ROOT_DIR/.data/gateway_jwt_secret"

# ensure_jwt_secret — guarantee the truth file exists. Idempotent; cheap
# enough (one -f test) for every Makefile target to run, including the
# read-only ones. On first creation an already-exported GW_JWT_SECRET is
# adopted rather than silently replaced. Does NOT export the variable —
# callers decide (dev-start.sh exports it; the Makefile reads the file).
ensure_jwt_secret() {
  if [ ! -f "$JWT_SECRET_FILE" ]; then
    mkdir -p "$(dirname "$JWT_SECRET_FILE")"
    if [ -n "${GW_JWT_SECRET:-}" ]; then
      printf '%s\n' "$GW_JWT_SECRET" > "$JWT_SECRET_FILE"
      dev_info "adopted GW_JWT_SECRET from the environment into ${JWT_SECRET_FILE#"$ROOT_DIR"/}"
    else
      openssl rand -base64 48 > "$JWT_SECRET_FILE"
      dev_info "generated GW_JWT_SECRET (${JWT_SECRET_FILE#"$ROOT_DIR"/})"
    fi
    chmod 600 "$JWT_SECRET_FILE"
  fi
}

# check_port_open HOST PORT — TCP reachability probe (nc). No output.
check_port_open() {
  nc -z -w 2 "$1" "$2" >/dev/null 2>&1
}

# ensure_infra — bring up PostgreSQL + Redis via Docker when not reachable.
#
# Dev storage contract (conf/platform.dev.yaml): PostgreSQL on localhost:5432
# and Redis on localhost:6379, both provided by deploy/docker-compose.infra.yml.
# When either is already reachable (brew services, a manual postgres, another
# compose project) we use it as-is; otherwise we start the compose project.
# This makes `make dev` a genuine one-command entry point on a machine with
# nothing but Docker installed.
#
# INFRA_MODE=none disables the auto-start for setups that manage their own
# storage (the reachability probe still gates startup).
ensure_infra() {
  # The truth file must exist BEFORE compose touches the postgres container:
  # its POSTGRES_PASSWORD_FILE is a single-file bind mount, and Docker turns a
  # missing host file into a DIRECTORY — which breaks the entrypoint with an
  # error far removed from the cause.
  ensure_pg_password

  if check_port_open localhost 5432 && check_port_open localhost 6379; then
    dev_ok "PostgreSQL (5432) and Redis (6379) reachable"
    verify_pg_password
    return 0
  fi

  if [ "${INFRA_MODE:-auto}" = "none" ]; then
    dev_die "PostgreSQL (localhost:5432) or Redis (localhost:6379) unreachable, and INFRA_MODE=none forbids starting them.
  Start your own, or re-run without INFRA_MODE=none."
  fi

  detect_compose
  [ -n "$DOCKER_COMPOSE" ] || dev_die "neither PostgreSQL nor Redis is reachable and no docker compose found.
  Start them yourself, or install Docker."

  dev_info "starting dev infrastructure via Docker (postgres, redis)"
  (cd "$ROOT_DIR" && $DOCKER_COMPOSE -f deploy/docker-compose.infra.yml up -d postgres redis)

  # Wait for both to accept connections. compose healthchecks gate `up -d`
  # only with --wait; polling here keeps the dependency visible in THIS log.
  local waited=0
  until check_port_open localhost 5432 && check_port_open localhost 6379; do
    waited=$((waited + 1))
    [ "$waited" -ge 60 ] && dev_die "dev infrastructure did not become reachable within 60s (docker logs datacode-postgres / datacode-redis)"
    sleep 1
  done
  # Healthy != authenticated: catch a volume initialized with a different
  # password here, where the message says what to do, instead of inside a
  # service crash-loop three layers away.
  verify_pg_password
  dev_ok "infrastructure ready (${waited}s)"
}

# run_db_migrations — apply every service's schema migrations once.
#
# One place, so "did I migrate?" can never have three answers. Order matters:
# control-service owns the platform alembic chain AND the control-plane base
# tables (python -m src.db_migrate creates them, then migrates — alembic's
# 0001 baseline is empty on purpose); identity-service owns the identity
# tables and refuses to SERVE while migrations are pending. Both run before
# any service starts, which is also what keeps runtime's init_db (it creates
# only its own three tables) from ever racing control's create_all.
# Idempotent by construction (create_all is checkfirst, alembic stamps head,
# identity's runner skips applied versions), so calling it on every dev start
# is the correct default.
run_db_migrations() {
  dev_info "applying database migrations (control create_all + alembic + identity)"
  local db_password="${DB_PASSWORD:-}"
  [ -n "$db_password" ] || dev_die "run_db_migrations: DB_PASSWORD is required (normally exported from .data/postgres_password by ensure_pg_password)"

  (cd "$ROOT_DIR/control-service" && \
    DB_PASSWORD="$db_password" ./.venv/bin/python -m src.db_migrate) \
    || dev_die "control-service migration failed — run 'make db-upgrade' with a reachable database to see the full error"

  # identity migrations run at boot under AUTO_MIGRATE, which dev-start.sh sets;
  # nothing to do here. Kept explicit so the ordering contract is documented.
  dev_ok "migrations applied"
}

# ── Readiness gates ──────────────────────────────────────────────────────

# wait_http LABEL URL TIMEOUT_S [LOGFILE] — block until URL answers 2xx/3xx.
# Aborts early when any already-started service process group has died
# (surface the real crash instead of timing out on a zombie port), and on
# failure prints the tail of LOGFILE before returning 1.
wait_http() {
  local label="$1" url="$2" timeout="$3" logfile="${4:-}"
  local waited=0 i dead
  while [ "$waited" -lt "$timeout" ]; do
    if curl -fsS -m 2 -o /dev/null "$url" 2>/dev/null; then
      dev_ok "$label ready (${waited}s)"
      return 0
    fi
    if [ "${#SVC_NAMES[@]}" -gt 0 ]; then
      dead=""
      for i in "${!SVC_NAMES[@]}"; do
        if ! kill -0 -- "-${SVC_PGIDS[$i]}" 2>/dev/null; then
          dev_warn "${SVC_NAMES[$i]} exited while waiting for $label; last lines:"
          tail -n 20 "$LOG_DIR/${SVC_NAMES[$i]}.log" 2>/dev/null | sed 's/^/    /' >&2
          dead="yes"
        fi
      done
      if [ -n "$dead" ]; then
        return 1
      fi
    fi
    sleep 1
    waited=$((waited + 1))
  done
  dev_warn "$label NOT ready at $url within ${timeout}s"
  if [ -n "$logfile" ] && [ -f "$logfile" ]; then
    dev_warn "last lines of ${logfile#"$ROOT_DIR"/}:"
    tail -n 25 "$logfile" 2>/dev/null | sed 's/^/    /' >&2
  fi
  return 1
}

# ── Dependency bootstrap ─────────────────────────────────────────────────

# ensure_go_deps SVC — run `go mod tidy` when SVC/go.sum is missing.
ensure_go_deps() {
  local svc="$1"
  if [ ! -f "$ROOT_DIR/$svc/go.sum" ]; then
    dev_info "[$svc] go mod tidy"
    (cd "$ROOT_DIR/$svc" && go mod tidy)
  fi
}

# ensure_python_venv SVC PROBE [EXTRAS] — guarantee SVC/.venv exists, runs
# Python >= 3.11, and can execute PROBE (an import statement). The probe is
# per-service on purpose: a shared probe like "import fastapi" proves nothing
# about sqlmesh or openai-agents being installed.
#
# EXTRAS defaults to `dev`. Services that promise more than the dev group to
# the code they host pass the extras they need — workspace-runtime installs
# `[dev,usercode]` because user assets are imported by ITS interpreter, so a
# dev venv without those packages is a venv where ordinary assets fail.
ensure_python_venv() {
  local svc="$1" probe="$2" extras="${3:-dev}"
  local dir="$ROOT_DIR/$svc"
  local vpy="$dir/.venv/bin/python"

  if [ ! -x "$vpy" ]; then
    local py
    py="$(find_python3)" || dev_die "[$svc] no Python >= 3.11 found — install one (3.13 recommended)"
    dev_info "[$svc] creating .venv with $py"
    (cd "$dir" && "$py" -m venv .venv)
  fi

  # Version guard first: a venv built with Python < 3.11 makes pip SILENTLY
  # skip every dependency (requires-python >= 3.11), and the resulting import
  # errors mislead. Fail loudly with the fix instead.
  if ! "$vpy" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    local pyname
    pyname="$(find_python3 || echo python3.13)"
    dev_die "[$svc] .venv is '$("$vpy" --version 2>&1)' but >= 3.11 is required. Recreate it:
  rm -rf $dir/.venv && $pyname -m venv $dir/.venv"
  fi

  if ! "$vpy" -c "$probe" >/dev/null 2>&1; then
    dev_info "[$svc] installing dependencies"
    # `-m pip`, not .venv/bin/pip: uv-created venvs ship pip3/pip3.11 only.
    "$vpy" -m pip install -q -e "$ROOT_DIR/shared/python"
    (cd "$dir" && "$vpy" -m pip install -q -e ".[$extras]")
  fi
}

# ── Per-service venv probes (ONE definition) ──────────────────────────────
# What "a healthy venv for service X" means. Shared by dev-setup.sh and
# dev-start.sh through ensure_service_venvs below — these lists used to live
# inline in BOTH entry points and drifted apart exactly the way drift always
# does: dev-setup moved to asyncpg when dev switched to PostgreSQL, dev-start
# kept aiomysql, and an older venv kept "passing" the stale probe without
# the PostgreSQL driver — the service then crash-looped at startup with
# ModuleNotFoundError. A probe must name the packages whose absence breaks
# BOOT (the PostgreSQL driver is core in every pyproject now, so it belongs
# here), never a retired dialect's driver.
VENV_PROBE_control="import fastapi, alembic, asyncpg, psycopg2, datacode_common"
VENV_PROBE_agent="import agents, grpc, asyncpg, datacode_common"
# `daft` is core (the asset-dag the service SCAFFOLDS into every working copy
# computes features with it), so it belongs in the probe even though its
# absence breaks nothing at boot: it fails only when one example asset is
# materialized, and a venv built before it was declared would otherwise never
# be repaired. That quiet failure is exactly what the probe exists to catch.
VENV_PROBE_runtime="import sqlmesh, dagster, asyncpg, daft, datacode_common, sklearn, scipy, pandas, pyarrow, numpy, importlib.util as _u; assert _u.find_spec('python_multipart') or _u.find_spec('multipart')"

# ensure_service_venvs — every Python service's venv, via the probes above.
# Nothing probes inline any more: a new entry point gets this contract for
# free instead of copying (and aging) its own list.
ensure_service_venvs() {
  ensure_python_venv control-service   "$VENV_PROBE_control"
  ensure_python_venv agent-service     "$VENV_PROBE_agent"
  # `usercode`, not just `dev`: user assets are imported by this service's
  # interpreter, so those packages are part of what "a healthy venv" means
  # here. Omitting them is not a smaller dev install, it is a dev environment
  # where ordinary assets fail to import.
  #
  # RUNTIME_EXTRAS mirrors the image build-arg of the same name (see
  # scripts/build-images.sh): `RUNTIME_EXTRAS=spark scripts/dev-start.sh` gives
  # the dev baseline the packages a `--build-arg RUNTIME_EXTRAS=spark` image has,
  # so "pyspark is provided by the platform" means the same thing in both. The
  # probe above deliberately does not name them — an existing venv that satisfies
  # it is never reinstalled, so set this before the venv exists, or delete it.
  #
  # `k8s` is NOT appended on purpose: dev runs the local task backend and never
  # talks to a cluster. The image installs it unconditionally (one image, every
  # deployment), which is a difference between the two and not a drift.
  ensure_python_venv workspace-runtime "$VENV_PROBE_runtime" \
    "dev,usercode${RUNTIME_EXTRAS:+,$RUNTIME_EXTRAS}"
}

# ── Ports & processes ────────────────────────────────────────────────────

# port_pids PORT — PIDs holding a LISTEN socket on PORT (one per line).
# -sTCP:LISTEN matters: without it lsof also matches sockets whose FOREIGN
# port equals PORT, i.e. clients talking to our services.
port_pids() {
  lsof -t -n -i ":$1" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

# svc_cwd PID — working directory of PID ("" when undeterminable).
svc_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

# is_dev_stack_pid PID — decide whether PID is provably one of OURS:
#   * a tree launched by dev-svc-runner.py — the process-group leader's
#     command mentions the runner;
#   * a service started by hand from this checkout — uvicorn src.main:app,
#     a go-build temp binary, or vite, each with its working directory
#     inside ROOT_DIR (the cwd check keeps other projects' identical-looking
#     processes safe).
is_dev_stack_pid() {
  local pid="$1" pgid cmd
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"

  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ]; then
    case "$(ps -o command= -p "$pgid" 2>/dev/null || true)" in
      *dev-svc-runner*) return 0 ;;
    esac
  fi

  case "$cmd" in
    *uvicorn\ src.main:app*|*go-build*|*vite*|*dagster-webserver*|*dagster-daemon*)
      # The Dagster entries are CHILDREN of workspace-runtime: it spawns them
      # with start_new_session=True, so they live in their own session and are
      # not covered by the group-leader check above. They inherit the runtime's
      # working directory (inside this checkout), which is what keeps another
      # project's identically named process safe.
      case "$(svc_cwd "$pid")" in
        "$ROOT_DIR"|"$ROOT_DIR"/*) return 0 ;;
      esac
      ;;
  esac
  return 1
}

# port_pids_ours PORT — PIDs listening on PORT that belong to the dev stack
# (see is_dev_stack_pid). Used by dev-stop.sh to wait for OUR listeners to
# go away without being held up by foreign squatters.
port_pids_ours() {
  local pid
  for pid in $(port_pids "$1"); do
    if is_dev_stack_pid "$pid"; then
      printf '%s\n' "$pid"
    fi
  done
}

# kill_port_group PORT LABEL [SIGNAL=TERM] — free PORT from dev-stack
# processes.
#
# Recognition is deliberate: only processes that are provably OURS are
# signalled. Anything else squatting on a dev port — an `ssh -L` forward,
# Docker's port proxy, a random daemon — is REPORTED, not killed: silently
# terminating unrelated infrastructure because it happens to sit on a dev
# port is how a stop script earns a bad name.
#
# For our own processes the signal goes to the listener's whole process
# group when that group is foreign to the caller (that is how
# dev-svc-runner.py launches services: group == runner PID, spanning
# go-run/binary or uvicorn-reloader/worker). Falls back to the single PID
# when the listener shares the caller's own group (a plain shell job) —
# group-killing that would terminate the caller's shell.
kill_port_group() {
  local port="$1" label="$2" sig="${3:-TERM}"
  local pid pids pgid caller_group

  pids="$(port_pids "$port")"
  [ -z "$pids" ] && return 0

  caller_group="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')"
  local seen_groups=""
  for pid in $pids; do
    case "$(ps -o comm= -p "$pid" 2>/dev/null || true)" in
      *docker*|*Docker*)
        dev_warn "port $port ($label) is held by Docker — stop the containerized stack instead: make stack-down"
        continue
        ;;
    esac
    if ! is_dev_stack_pid "$pid"; then
      dev_warn "port $port ($label) is held by a non-dev-stack process (pid $pid: $(ps -o comm= -p "$pid" 2>/dev/null || echo '?')) — NOT stopping it; free the port manually if you need it"
      continue
    fi
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [ -n "$pgid" ] && [ "$pgid" != "$caller_group" ]; then
      # One signal per process group: a uvicorn --reload pair (reloader +
      # worker) shares the group and needs a single TERM, not two.
      case " $seen_groups " in
        *" $pgid "*) continue ;;
      esac
      seen_groups="$seen_groups $pgid"
      dev_info "stopping $label on :$port (pid $pid, process group $pgid)"
      kill "-$sig" -- "-$pgid" 2>/dev/null || kill "-$sig" "$pid" 2>/dev/null || true
    else
      dev_info "stopping $label on :$port (pid $pid)"
      kill "-$sig" "$pid" 2>/dev/null || true
    fi
  done
}

# ── Service lifecycle ────────────────────────────────────────────────────
SVC_NAMES=()
SVC_PGIDS=()

# start_svc NAME DIR CMD [ARGS...] — launch CMD in DIR through
# dev-svc-runner.py. The runner calls setsid(), so its PID (captured below)
# is also the PGID of the entire service tree; output is mirrored to the
# console with a "[NAME] " prefix and to $LOG_DIR/NAME.log (per run).
start_svc() {
  local name="$1" dir="$2"
  shift 2
  dev_info "starting $name (log: .data/logs/$name.log)"
  ( exec python3 "$SCRIPT_DIR/dev-svc-runner.py" \
      --name "$name" --log "$LOG_DIR/$name.log" --cwd "$dir" -- "$@" ) &
  local pgid="$!"
  # The runner calls setsid() a few milliseconds after exec; until then $!
  # is not yet a process-group id and liveness probes would false-negative
  # (a "dead" verdict that orphans a booting service). Wait — bounded — for
  # the group to come into existence.
  local w=0
  until kill -0 -- "-$pgid" 2>/dev/null; do
    if ! kill -0 "$pgid" 2>/dev/null; then
      break   # runner died before detaching; wait_http will report it
    fi
    sleep 0.1
    w=$((w + 1))
    [ "$w" -ge 20 ] && break
  done
  SVC_NAMES+=("$name")
  SVC_PGIDS+=("$pgid")
}

# stop_svc_groups — TERM every started service group, wait up to 10s for a
# graceful exit, then SIGKILL the survivors. Idempotent.
stop_svc_groups() {
  if [ "${#SVC_PGIDS[@]}" -eq 0 ]; then
    return 0
  fi
  local pgid round alive
  for pgid in "${SVC_PGIDS[@]}"; do
    kill -TERM -- "-$pgid" 2>/dev/null || true
  done
  for round in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    alive=""
    for pgid in "${SVC_PGIDS[@]}"; do
      if kill -0 -- "-$pgid" 2>/dev/null; then
        alive="yes"
      fi
    done
    [ -z "$alive" ] && return 0
    sleep 0.5
  done
  for pgid in "${SVC_PGIDS[@]}"; do
    kill -KILL -- "-$pgid" 2>/dev/null || true
  done
}
