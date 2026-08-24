#!/usr/bin/env bash
# ============================================================================
# dc.sh — DataCode platform-domain CLI (dc = DataCode)
# ============================================================================
# Command model (all intercepted commands hit the dagster-service REST API):
#   dc assets [--json]                  → asset dependency graph (GET graph)
#   dc deployment status [--json]       → idempotent ensure + deployment state
#   dc deployment reload                → re-import code after asset-dag/ edits
#   dc run <job_name> [--config-json J] → launch a job run in this environment
#
# `dc` also happens to be the POSIX desk calculator. The shim directory is
# prepended to PATH by the shell tool, so it would shadow the system dc;
# any subcommand we do not know is forwarded to the REAL dc binary
# (resolution mirrors _resolve_real_sqlmesh in sqlmesh.sh: strip the shim
# directory from PATH, then `command -v dc`, fallback /usr/bin/dc).
#
# Environment governance:
#   DATACODE_ENVIRONMENT, when injected by the shell tool, is sent as the
#   `environment` CONFIRMATION field on deployment ensure/reload — the
#   backend rejects a mismatch with the working copy (anti-hallucination,
#   same semantics as sqlmesh.sh). env_type / schedules_enabled are
#   intentionally NOT sent: they carry the environment registry's policy
#   and default safely on the backend.
#
# Environment variables (injected by shell.py or set manually):
#   DATACODE_DAGSTER_SERVICE_URL  Dagster service base URL
#                                    (default: http://localhost:8004)
#   DATACODE_WORKSPACE_PATH       Absolute path to the workspace root
#   DATACODE_ENVIRONMENT          Authoritative active environment
#
# Exit codes: 0 = success, 1 = usage error, 2 = HTTP error, 3 = invalid
# JSON, 127 = cannot locate the real dc binary.
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=dc-lib.sh
source "${SCRIPT_DIR}/dc-lib.sh"

# -------------------------------------------------------------------
# Resolve backend API base URL
# -------------------------------------------------------------------
API_URL="${DATACODE_DAGSTER_SERVICE_URL:-http://localhost:8004}"

# -------------------------------------------------------------------
# Resolve the REAL system dc binary (avoid self-recursion)
# -------------------------------------------------------------------
_dc_resolve_real_dc() {
    local cleaned_path=""
    local IFS=':'
    for dir in $PATH; do
        if [[ "$dir" != "$SCRIPT_DIR" ]]; then
            if [[ -z "$cleaned_path" ]]; then
                cleaned_path="$dir"
            else
                cleaned_path="${cleaned_path}:${dir}"
            fi
        fi
    done

    local real_bin
    real_bin=$(PATH="$cleaned_path" command -v dc 2>/dev/null || true)

    if [[ -z "$real_bin" && -x /usr/bin/dc ]]; then
        real_bin="/usr/bin/dc"
    fi

    if [[ -z "$real_bin" ]]; then
        echo "[dc] ERROR: Cannot locate the system 'dc' binary." >&2
        echo "[dc] Tried PATH (excluding self): ${cleaned_path}" >&2
        exit 127
    fi

    echo "$real_bin"
}

_dc_passthrough() {
    local real_dc
    real_dc=$(_dc_resolve_real_dc)
    echo "[dc] Passthrough → system dc: dc $*" >&2
    exec "$real_dc" "$@"
}

_dc_usage() {
    cat >&2 <<'EOF'
Usage: dc <command> [options]

DataCode platform commands (dagster-service):
  assets [--json]                  Asset dependency graph of the active environment
  deployment status [--json]       Dagster deployment state (idempotent ensure)
  deployment reload                Re-import orchestration code after asset-dag/ edits
  run <job_name> [--config-json J] Launch a job run in the active environment

Anything else is forwarded to the system `dc` (desk calculator).
EOF
}

# ===================================================================
# Command handlers
# ===================================================================

# -------------------------------------------------------------------
# _dc_handle_assets — GET /api/v1/dagster/assets/graph
#
# The graph is scoped to the deployment of the working copy. The
# environment query parameter makes the requested scope explicit in
# backend logs (the working copy remains authoritative).
# -------------------------------------------------------------------
_dc_handle_assets() {
    local workspace_path env_name json_output="false"
    workspace_path=$(_dc_require_workspace)
    env_name=$(_dc_resolve_env)
    if _dc_has_flag "--json" "$@"; then
        json_output="true"
    fi

    _DC_API_URL="$API_URL" _DC_WORKSPACE="$workspace_path" _DC_ENV="$env_name" \
    _DC_JSON_OUTPUT="$json_output" \
        python3 -c "
import json, os, sys, urllib.parse

api_url     = os.environ.get('_DC_API_URL', 'http://localhost:8004')
workspace   = os.environ.get('_DC_WORKSPACE', '')
env_name    = os.environ.get('_DC_ENV', '')
json_output = os.environ.get('_DC_JSON_OUTPUT', 'false') == 'true'

# --- Build request ---
method = 'GET'
query = 'workspace_path=' + urllib.parse.quote(workspace, safe='')
if env_name:
    query += '&environment=' + urllib.parse.quote(env_name, safe='')
url = api_url + '/api/v1/dagster/assets/graph?' + query
request_body = ''

${_DC_PY_TRANSPORT}

# === Response handler: asset graph ===
if json_output:
    print(json.dumps(r, indent=2))
    sys.exit(0)

nodes = r.get('nodes', [])
edges = r.get('edges', [])
print(f'Asset graph: {len(nodes)} assets, {len(edges)} dependencies')
for node in nodes:
    name  = node.get('name') or node.get('asset_key_str') or '?'
    group = node.get('group_name') or '-'
    deps  = node.get('deps') or []
    dep_str = ', '.join(deps)
    if dep_str:
        print(f'  [{group}] {name}  <- {dep_str}')
    else:
        print(f'  [{group}] {name}')
sys.exit(0)
"
}

# -------------------------------------------------------------------
# _dc_handle_deployment_action — POST /api/v1/dagster/deployments/{action}
#
# action=ensure backs `deployment status`: idempotent — an already-running
# code location is NOT respawned, only its policy is refreshed, so it is
# safe to call for a status read.
# action=reload backs `deployment reload`: re-imports the working copy's
# code after asset-dag/ edits. Explicit by design — nothing watches the
# filesystem.
# -------------------------------------------------------------------
_dc_handle_deployment_action() {
    local action="$1"
    shift
    local workspace_path env_name json_output="false"
    workspace_path=$(_dc_require_workspace)
    env_name=$(_dc_resolve_env)
    if _dc_has_flag "--json" "$@"; then
        json_output="true"
    fi

    _DC_API_URL="$API_URL" _DC_WORKSPACE="$workspace_path" _DC_ENV="$env_name" \
    _DC_JSON_OUTPUT="$json_output" _DC_ACTION="$action" \
        python3 -c "
import json, os, sys

api_url     = os.environ.get('_DC_API_URL', 'http://localhost:8004')
workspace   = os.environ.get('_DC_WORKSPACE', '')
env_name    = os.environ.get('_DC_ENV', '')
json_output = os.environ.get('_DC_JSON_OUTPUT', 'false') == 'true'
action      = os.environ.get('_DC_ACTION', 'ensure')

# --- Build request ---
# environment is a CONFIRMATION field: a mismatch with the working copy is
# rejected by the backend. env_type / schedules_enabled stay unset — they
# carry registry policy and default safely server-side.
method = 'POST'
url = api_url + '/api/v1/dagster/deployments/' + action
payload = {'workspace_path': workspace}
if env_name:
    payload['environment'] = env_name
request_body = json.dumps(payload)

${_DC_PY_TRANSPORT}

# === Response handler: deployment info ===
if json_output:
    print(json.dumps(r, indent=2))
    sys.exit(0)

deployment = r.get('deployment_name', '?')
environment = r.get('environment', '?')
env_type = r.get('env_type', '?')
alive = r.get('alive', False)
host = r.get('host', '?')
port = r.get('port', '?')
schedules = r.get('schedules_enabled', False)
location = r.get('location_name') or deployment

state = 'RUNNING' if alive else 'NOT RUNNING'
sched_state = 'enabled' if schedules else 'disabled'
if action == 'reload':
    print(f'Deployment reloaded: {deployment}')
else:
    print(f'Deployment: {deployment}')
print(f'  Environment    : {environment} ({env_type})')
print(f'  Code location  : {location} — {state} at {host}:{port}')
print(f'  Schedules      : {sched_state}')
sys.exit(0)
"
}

_dc_handle_deployment() {
    local sub="${1:-}"
    case "$sub" in
        status) shift; _dc_handle_deployment_action "ensure" "$@" ;;
        reload) shift; _dc_handle_deployment_action "reload" "$@" ;;
        *)
            echo "[dc] ERROR: Unknown deployment subcommand: '${sub:-<empty>}'" >&2
            echo "[dc] Usage: dc deployment {status|reload}" >&2
            exit 1
            ;;
    esac
}

# -------------------------------------------------------------------
# _dc_handle_run — POST /api/v1/dagster/pipelines/run
#
# The backend ensures the deployment first and stamps the run with the
# deployment's tags, so the execution is answerable for the environment
# that produced it. TriggerRunRequest has no environment field — the
# working copy encoded in workspace_path selects the deployment.
# -------------------------------------------------------------------
_dc_handle_run() {
    local job_name="" config_json=""
    local prev=""
    for arg in "$@"; do
        case "$prev" in
            --config-json) config_json="$arg" ;;
        esac
        if [[ "$arg" != "--config-json" && -z "$job_name" && "$arg" != -* ]]; then
            job_name="$arg"
        fi
        prev="$arg"
    done

    if [[ -z "$job_name" ]]; then
        echo "[dc] ERROR: job_name is required." >&2
        echo "[dc] Usage: dc run <job_name> [--config-json '<json>']" >&2
        exit 1
    fi

    local workspace_path
    workspace_path=$(_dc_require_workspace)

    _DC_API_URL="$API_URL" _DC_WORKSPACE="$workspace_path" \
    _DC_JOB_NAME="$job_name" _DC_CONFIG_JSON="$config_json" \
        python3 -c "
import json, os, sys

api_url    = os.environ.get('_DC_API_URL', 'http://localhost:8004')
workspace  = os.environ.get('_DC_WORKSPACE', '')
job_name   = os.environ.get('_DC_JOB_NAME', '')
config_raw = os.environ.get('_DC_CONFIG_JSON', '')

# --- Build request ---
payload = {
    'workspace_path': workspace,
    'job_name': job_name,
}
if config_raw:
    try:
        payload['run_config'] = json.loads(config_raw)
    except Exception:
        print('[dc] ERROR: --config-json is not valid JSON', file=sys.stderr)
        sys.exit(1)
request_body = json.dumps(payload)

method = 'POST'
url = api_url + '/api/v1/dagster/pipelines/run'

${_DC_PY_TRANSPORT}

# === Response handler: run info ===
run_id = r.get('run_id', '?')
status = r.get('status', '?')
print(f'Run launched: {job_name}')
print(f'  Run ID : {run_id}')
print(f'  Status : {status}')
sys.exit(0)
"
}

# ===================================================================
# Main dispatch logic
# ===================================================================
SUBCOMMAND="${1:-}"

case "$SUBCOMMAND" in
    assets)
        shift
        _dc_handle_assets "$@"
        ;;
    deployment)
        shift
        _dc_handle_deployment "$@"
        ;;
    run)
        shift
        _dc_handle_run "$@"
        ;;
    "")
        _dc_usage
        exit 1
        ;;
    -h|--help)
        _dc_usage
        exit 0
        ;;
    *)
        # Unknown subcommand — forward to the system desk calculator so the
        # shim never shadows it (e.g. `dc -e '2 2 + p'` still works).
        _dc_passthrough "$@"
        ;;
esac
