#!/usr/bin/env bash
# ============================================================================
# sqlmesh.sh — HUMAN-FACING CLI wrapper for sqlmesh
# ============================================================================
# WHO THIS IS FOR
#   A person working in a terminal on a workspace they have open. It is NOT on
#   the agent's path any more and must not be put back there.
#
#   Governed verbs used to be dispatched from INSIDE the executing side: this
#   shim was prepended to the agent sandbox's PATH and called the platform back
#   over HTTP. That required handing the sandbox the platform's address and the
#   caller's identity — the exact network path the L3-base NetworkPolicy denies
#   (threats T3/T6) — and it made dev and production decide differently, because
#   the deciding factor was whether the shim could be injected at all.
#
#   Agent commands are now ROUTED by workspace-runtime before anything executes
#   (workspace-runtime/src/tools/command_router.py). The verb list both sides
#   agree on lives in shared/python/datacode_common/command_verbs.py; a sandbox
#   additionally carries generated guards that refuse those verbs, for the case
#   routing cannot cover (a governed verb inside a compound command).
#
#   This file therefore serves one remaining audience: a human whose shell has
#   the platform reachable. Keep it in sync with command_verbs.py when the set
#   of governed verbs changes.
#
# Command model:
#   sqlmesh plan <env>   → preview (intercepted → backend API POST /api/v1/plan).
#                          Shows diff summary; never deploys. Read-only.
#   sqlmesh apply <env>  → deploy (intercepted → backend API POST /api/v1/apply)
#                          with unified platform governance (locking, optimistic
#                          concurrency, and apply history). Must only be invoked
#                          after the user has confirmed the deployment.
#
# Both plan and apply accept the same CLI flags as the native sqlmesh binary
# (--skip-tests, --forward-only, --start, --end, etc.). The legacy
# "plan --auto-apply" is intentionally rejected.
#
# Ontology commands (intercepted → the runtime's ontology API on port 8002):
#   sqlmesh ontology validate <name> → validate cross-references in one ontology
#                                      (POST /api/v1/ontology/{name}/validate).
#   sqlmesh ontology validate --all  → validate every ontology in the workspace.
#   sqlmesh ontology list            → list all ontologies (GET /api/v1/ontology).
#
# All other read-only / local commands are transparently forwarded to the
# native sqlmesh binary. Before forwarding, the shim locates the SQLMesh
# project directory ({workspace}/sql-warehouse/ under the platform layout)
# and execs the native binary from there, because a shell places us at the
# worktree root while native sqlmesh expects cwd inside the project.
#
#   sqlmesh run ...     → REJECTED. Data refresh is a write operation that
#                          would bypass platform governance (environment
#                          guardrail, lock, audit). Land data with plan/apply,
#                          or trigger an orchestrated refresh with
#                          `dc run <job_name>`.
#
# Environment variables (set manually by the user, or by their shell profile):
#   DATACODE_WORKSPACE_RUNTIME_URL  Backend (workspace-runtime) API base URL
#                                   (default: http://localhost:8002)
#   DATACODE_ONTOLOGY_SERVICE_URL Ontology service base URL (default: http://localhost:8002)
#   DATACODE_WORKSPACE_PATH       Absolute path to workspace root
#   DATACODE_ENVIRONMENT          Authoritative active environment, when the
#                                    caller wants one pinned. Overrides the
#                                    environment passed to plan/apply.
#   DATACODE_TRIGGER_TYPE         "agent" or "human" (default: human)
#   DATACODE_USER_ID              User identifier (default: $USER)
#   DATACODE_SESSION_ID           Session identifier (agent context only)
#   DATACODE_REAL_SQLMESH         Optional explicit path to real sqlmesh binary
# ============================================================================

set -euo pipefail

# -------------------------------------------------------------------
# Resolve the real sqlmesh binary (avoid self-recursion)
# -------------------------------------------------------------------
_resolve_real_sqlmesh() {
    if [[ -n "${DATACODE_REAL_SQLMESH:-}" ]] && command -v "${DATACODE_REAL_SQLMESH}" &>/dev/null; then
        echo "${DATACODE_REAL_SQLMESH}"
        return
    fi

    local script_dir
    script_dir="$(cd "$(dirname "$0")" && pwd)"

    local cleaned_path=""
    local IFS=':'
    for dir in $PATH; do
        if [[ "$dir" != "$script_dir" ]]; then
            if [[ -z "$cleaned_path" ]]; then
                cleaned_path="$dir"
            else
                cleaned_path="${cleaned_path}:${dir}"
            fi
        fi
    done

    local real_bin
    real_bin=$(PATH="$cleaned_path" command -v sqlmesh 2>/dev/null || true)

    if [[ -z "$real_bin" ]]; then
        echo "[sqlmesh.sh] ERROR: Cannot locate the real sqlmesh binary." >&2
        echo "[sqlmesh.sh] Tried PATH (excluding self): ${cleaned_path}" >&2
        echo "[sqlmesh.sh] Set DATACODE_REAL_SQLMESH to explicitly specify the binary path." >&2
        exit 127
    fi

    echo "$real_bin"
}

REAL_SQLMESH=$(_resolve_real_sqlmesh)

# -------------------------------------------------------------------
# Resolve backend API base URL
# -------------------------------------------------------------------
API_URL="${DATACODE_WORKSPACE_RUNTIME_URL:-http://localhost:8002}"

# workspace-runtime (8002) owns the working-copy surface since P3; the
# ontology data plane moved into it as well.
ONTOLOGY_API_URL="${DATACODE_ONTOLOGY_SERVICE_URL:-http://localhost:8002}"

# -------------------------------------------------------------------
# Resolve workspace path (required for backend API commands)
# -------------------------------------------------------------------
_resolve_workspace_path() {
    if [[ -n "${DATACODE_WORKSPACE_PATH:-}" ]]; then
        echo "${DATACODE_WORKSPACE_PATH}"
        return
    fi
    # Preferred: platform layout — walk up to the directory whose
    # sql-warehouse/ sub-directory holds the SQLMesh config. This yields the
    # WORKTREE ROOT expected by the backend's validate_workspace_path, even
    # when cwd is deep inside the project.
    local d="$PWD"
    while [[ "$d" != "/" ]]; do
        if [[ -f "$d/sql-warehouse/config.yaml" ]]; then
            echo "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    # Fallback: bare SQLMesh project — walk up to the directory that contains
    # config.yaml itself (standalone projects without the dual-directory
    # layout).
    d="$PWD"
    while [[ "$d" != "/" ]]; do
        if [[ -f "$d/config.yaml" ]]; then
            echo "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    echo ""
}

# -------------------------------------------------------------------
# Resolve the directory the native sqlmesh binary should run from.
#
# Passthrough commands (test / audit / render / dag / evaluate) exec the
# real sqlmesh binary, which needs cwd inside the project (where config.yaml
# lives). Under the platform layout the project is {workspace}/sql-warehouse/
# — one level below the worktree root where the shell tool places us — so
# descend into it before exec'ing. Bare projects and project-less calls
# (--version / --help) keep the current directory.
# -------------------------------------------------------------------
_resolve_sqlmesh_project_dir() {
    local workspace_path
    workspace_path=$(_resolve_workspace_path)
    if [[ -n "$workspace_path" && -f "${workspace_path%/}/sql-warehouse/config.yaml" ]]; then
        echo "${workspace_path%/}/sql-warehouse"
        return
    fi
    echo "$PWD"
}

# -------------------------------------------------------------------
# Resolve user identity
# -------------------------------------------------------------------
USER_ID="${DATACODE_USER_ID:-${USER:-unknown}}"
TRIGGER_TYPE="${DATACODE_TRIGGER_TYPE:-human}"
SESSION_ID="${DATACODE_SESSION_ID:-}"

# -------------------------------------------------------------------
# Check if an argument list contains a specific flag
# -------------------------------------------------------------------
_has_flag() {
    local flag="$1"
    shift
    for arg in "$@"; do
        if [[ "$arg" == "$flag" ]]; then
            return 0
        fi
    done
    return 1
}

# -------------------------------------------------------------------
# Extract the explicit environment argument (first non-flag positional
# after the leading subcommand, e.g. "apply dev" → "dev"). Returns an
# empty string when the caller did not specify one.
# -------------------------------------------------------------------
_extract_explicit_env() {
    local idx=0
    for arg in "$@"; do
        if [[ $idx -gt 0 && "$arg" != -* ]]; then
            echo "$arg"
            return
        fi
        idx=$((idx + 1))
    done
    echo ""
}

# -------------------------------------------------------------------
# Resolve the AUTHORITATIVE environment for plan/apply.
#
# Priority (anti-hallucination):
#   1. DATACODE_ENVIRONMENT — the environment the user selected in the
#      toolbar, injected by the agent shell tool. This is the single source
#      of truth and overrides whatever the LLM passed on the command line.
#   2. The explicit positional env argument — used only for direct human CLI
#      usage where DATACODE_ENVIRONMENT is not injected.
#   3. "dev" — conservative fallback when nothing is available.
# -------------------------------------------------------------------
_resolve_env() {
    if [[ -n "${DATACODE_ENVIRONMENT:-}" ]]; then
        echo "${DATACODE_ENVIRONMENT}"
        return
    fi
    local explicit
    explicit=$(_extract_explicit_env "$@")
    if [[ -n "$explicit" ]]; then
        echo "$explicit"
        return
    fi
    echo "dev"
}

# -------------------------------------------------------------------
# Parse --start and --end from CLI args
# -------------------------------------------------------------------
_extract_dates() {
    local start=""
    local end=""
    local prev=""
    for arg in "$@"; do
        case "$prev" in
            --start) start="$arg" ;;
            --end)   end="$arg" ;;
        esac
        prev="$arg"
    done
    echo "${start}|${end}"
}

# ===================================================================
# Backend API abstraction layer
# ===================================================================
# All commands routed to the backend API share the same env-var
# export pipeline and the same Python HTTP client. Only the endpoint
# URL and the response parser differ between commands.
#
# Adding a new backend command:
#   1. Create _handle_<cmd>() that calls:
#        _export_api_env "$@"
#        _invoke_backend_api "<endpoint>" "<auto_apply>"
#   2. Add a dispatch case in the main switch.
# ===================================================================

# -------------------------------------------------------------------
# _export_api_env — parse CLI args and export as _AD_* env vars.
#
# Populates a standard set of environment variables that the Python
# script inside _invoke_backend_api reads to build the JSON payload.
# Call this once before _invoke_backend_api.
# -------------------------------------------------------------------
_export_api_env() {
    local workspace_path env_name
    workspace_path=$(_resolve_workspace_path)
    if [[ -z "$workspace_path" ]]; then
        echo "[sqlmesh.sh] ERROR: Cannot determine workspace_path." >&2
        echo "[sqlmesh.sh] Set DATACODE_WORKSPACE_PATH or run from within a sqlmesh project." >&2
        exit 1
    fi
    env_name=$(_resolve_env "$@")

    export _AD_WORKSPACE="$workspace_path"
    export _AD_ENV="$env_name"
    export _AD_API_URL="$API_URL"
    export _AD_USER_ID="$USER_ID"
    export _AD_TRIGGER_TYPE="$TRIGGER_TYPE"
    export _AD_SESSION_ID="$SESSION_ID"

    # Parse --start / --end
    local dates_raw start_date end_date
    dates_raw=$(_extract_dates "$@")
    start_date="${dates_raw%%|*}"
    end_date="${dates_raw##*|}"
    export _AD_START_DATE="${start_date}"
    export _AD_END_DATE="${end_date}"

    # Parse boolean flags
    export _AD_SKIP_TESTS="false"
    export _AD_SKIP_BACKFILL="false"
    export _AD_NO_GAPS="false"
    export _AD_FORWARD_ONLY="false"
    export _AD_NO_AUTO_CAT="false"
    export _AD_INCLUDE_UNMOD="false"
    local restate_list='['
    local prev=""
    for arg in "$@"; do
        case "$arg" in
            --skip-tests)               export _AD_SKIP_TESTS="true" ;;
            --skip-backfill)            export _AD_SKIP_BACKFILL="true" ;;
            --no-gaps)                  export _AD_NO_GAPS="true" ;;
            --forward-only)             export _AD_FORWARD_ONLY="true" ;;
            --no-auto-categorization)   export _AD_NO_AUTO_CAT="true" ;;
            --include-unmodified)       export _AD_INCLUDE_UNMOD="true" ;;
        esac
        case "$prev" in
            --restate-model) restate_list+="\"$arg\"," ;;
        esac
        prev="$arg"
    done
    restate_list="${restate_list%,}]"
    if [[ "$restate_list" == "]" ]]; then
        export _AD_RESTATE_MODELS=""
    else
        export _AD_RESTATE_MODELS="$restate_list"
    fi
}

# -------------------------------------------------------------------
# _PY_TRANSPORT — shared Python snippet: POST `request_body` to
# `{api_url}{endpoint}` and parse the JSON response into `r`.
#
# Callers embed this snippet after defining the python variables
# `api_url`, `endpoint` and `request_body`, and consume `r` afterwards.
# Standard exit codes: 2 = HTTP/connection error, 3 = invalid JSON.
# -------------------------------------------------------------------
_PY_TRANSPORT="
import json, sys, urllib.request, urllib.error

try:
    _req = urllib.request.Request(
        f'{api_url}{endpoint}',
        data=request_body.encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(_req, timeout=300) as _resp:
        _response_body = _resp.read().decode('utf-8')
except urllib.error.HTTPError as e:
    _body = e.read().decode('utf-8') if e.fp else '{}'
    print(f'[sqlmesh.sh] ERROR: Backend returned HTTP {e.code}', file=sys.stderr)
    try:
        _err = json.loads(_body)
    except Exception:
        _err = None
    if isinstance(_err, dict):
        # Print every distinct human-readable chunk the backend provides, in
        # priority order, so the actual cause is ALWAYS shown. Previously only
        # the first non-empty field was printed, which hid the real error text
        # in 'detail' behind a generic 'message' (e.g. 'Failed to apply plan').
        #   - report:         rich native-CLI-style lint/plan diagnostics
        #   - message:        top-level reason (raw error for business failures)
        #   - detail_message: nested summary preserved by structured errors
        #   - detail:         raw error text kept for unexpected server faults
        _chunks = []
        for _field in ('report', 'message', 'detail_message', 'detail'):
            _val = _err.get(_field)
            if not _val:
                continue
            if isinstance(_val, dict):
                _val = json.dumps(_val, indent=2)
            _val = str(_val)
            if _val not in _chunks:  # de-duplicate identical fields
                _chunks.append(_val)
        if not _chunks:
            _chunks = [_body]
        for _chunk in _chunks:
            for _line in (_chunk.splitlines() or ['']):
                print(f'[sqlmesh.sh] {_line}', file=sys.stderr)
    else:
        print(f'[sqlmesh.sh] {_body}', file=sys.stderr)
    sys.exit(2)
except urllib.error.URLError as e:
    print(f'[sqlmesh.sh] ERROR: Cannot reach backend API at {api_url}', file=sys.stderr)
    print(f'[sqlmesh.sh] {e.reason}', file=sys.stderr)
    sys.exit(2)
except Exception as e:
    print(f'[sqlmesh.sh] ERROR: {e}', file=sys.stderr)
    sys.exit(3)

try:
    r = json.loads(_response_body)
except Exception:
    print('[sqlmesh.sh] ERROR: Invalid JSON response from backend', file=sys.stderr)
    sys.exit(3)
"

# -------------------------------------------------------------------
# _invoke_backend_api — call backend API via inline Python.
#
# Parameters:
#   $1  endpoint    e.g. "/api/v1/plan" or "/api/v1/apply"
#   $2  auto_apply  "true" or "false"  (whether to set auto_apply in plan_options)
#
# Requires _export_api_env to have been called first.
# -------------------------------------------------------------------
_invoke_backend_api() {
    local endpoint="$1"
    local auto_apply="$2"

    export _AD_ENDPOINT="$endpoint"
    export _AD_AUTO_APPLY="$auto_apply"

    local exit_code=0
    python3 -c "
import json, os, sys

# --- Resolve inputs from environment ---
workspace_path = os.environ.get('_AD_WORKSPACE', '')
env_name       = os.environ.get('_AD_ENV', 'dev')
api_url        = os.environ.get('_AD_API_URL', 'http://localhost:8002')
user_id        = os.environ.get('_AD_USER_ID', '')
trigger_type   = os.environ.get('_AD_TRIGGER_TYPE', 'human')
session_id     = os.environ.get('_AD_SESSION_ID', '')
start_date     = os.environ.get('_AD_START_DATE', '')
end_date       = os.environ.get('_AD_END_DATE', '')
skip_tests     = os.environ.get('_AD_SKIP_TESTS', 'false') == 'true'
skip_backfill  = os.environ.get('_AD_SKIP_BACKFILL', 'false') == 'true'
no_gaps        = os.environ.get('_AD_NO_GAPS', 'false') == 'true'
forward_only   = os.environ.get('_AD_FORWARD_ONLY', 'false') == 'true'
no_auto_cat    = os.environ.get('_AD_NO_AUTO_CAT', 'false') == 'true'
include_unmod  = os.environ.get('_AD_INCLUDE_UNMOD', 'false') == 'true'
restate_raw    = os.environ.get('_AD_RESTATE_MODELS', '')
auto_apply     = os.environ.get('_AD_AUTO_APPLY', 'false') == 'true'
endpoint       = os.environ.get('_AD_ENDPOINT', '/api/v1/apply')

# --- Build request payload ---
payload = {
    'workspace_path': workspace_path,
    'environment': env_name,
    'plan_options': {
        'skip_tests': skip_tests,
        'skip_backfill': skip_backfill,
        'no_gaps': no_gaps,
        'forward_only': forward_only,
        'no_auto_categorization': no_auto_cat,
        'include_unmodified': include_unmod,
    },
    'trigger_type': trigger_type,
    'user_id': user_id,
    'session_id': session_id,
}

if auto_apply:
    payload['plan_options']['auto_apply'] = True

if restate_raw:
    try:
        payload['plan_options']['restate_models'] = json.loads(restate_raw)
    except Exception:
        payload['plan_options']['restate_models'] = [restate_raw]

if start_date or end_date:
    pd = {}
    if start_date:
        pd['start'] = start_date
    if end_date:
        pd['end'] = end_date
    payload['plan_dates'] = pd

request_body = json.dumps(payload)
${_PY_TRANSPORT}

# === Response handler: plan preview ===
if endpoint == '/api/v1/plan':
    has_changes  = r.get('has_changes', False)
    changes      = r.get('changes', {})
    backfills    = r.get('backfills', [])
    fingerprint  = r.get('state_fingerprint', '')
    requires_backfill = r.get('requires_backfill', False)

    if not has_changes:
        print('No changes to apply.')
        sys.exit(0)

    added   = len(changes.get('added', []))
    removed = len(changes.get('removed', []))
    modified = changes.get('modified') or {}
    modified_direct   = len(modified.get('direct', []))
    modified_indirect = len(modified.get('indirect', []))
    modified_meta     = len(modified.get('metadata', []))

    print('Plan Summary:')
    print(f'  Added: {added}, Removed: {removed}')
    print(f'  Directly Modified: {modified_direct}')
    print(f'  Indirectly Modified: {modified_indirect}')
    print(f'  Metadata Changes: {modified_meta}')

    # Print changed model names with change_category
    if modified_direct > 0:
        print(f'\n  Directly Modified Models:')
        for ch in modified.get('direct', []):
            name = ch.get('view_name', ch.get('name', '?'))
            cat  = ch.get('change_category', '')
            cat_str = f' [{cat}]' if cat else ''
            print(f'    - {name}{cat_str}')

    if modified_indirect > 0:
        print(f'\n  Indirectly Modified Models:')
        for ch in modified.get('indirect', []):
            name = ch.get('view_name', ch.get('name', '?'))
            print(f'    - {name}')

    if requires_backfill:
        print(f'\n  Backfills ({len(backfills)}):')
        for bf in backfills:
            name   = bf.get('view_name', bf.get('name', '?'))
            bcount = bf.get('batches', 0)
            interval = bf.get('interval', [])
            intv_str = ', '.join(interval) if interval else ''
            print(f'    - {name}: {bcount} batch(es)  {intv_str}')

    if fingerprint:
        print(f'\n  State Fingerprint: {fingerprint}')

    sys.exit(0)

# === Response handler: apply ===
elif endpoint == '/api/v1/apply':
    applied      = r.get('applied', False)
    reason       = r.get('reason', 'unknown')
    has_changes  = r.get('has_changes', False)
    tests_failed = r.get('tests_failed', False)
    changes      = r.get('changes', {})
    backfills    = r.get('backfills', [])

    if applied:
        print('Changes applied successfully to environment.')
        added   = len(changes.get('added', []))
        removed = len(changes.get('removed', []))
        modified_direct   = len((changes.get('modified') or {}).get('direct', []))
        modified_indirect = len((changes.get('modified') or {}).get('indirect', []))
        modified_meta     = len((changes.get('modified') or {}).get('metadata', []))
        total = added + removed + modified_direct + modified_indirect + modified_meta
        print(f'  Added: {added}, Removed: {removed}')
        print(f'  Modified (direct): {modified_direct}, (indirect): {modified_indirect}, (metadata): {modified_meta}')
        print(f'  Backfills: {len(backfills)}')
        if backfills:
            for bf in backfills:
                name  = bf.get('name', '?')
                bcount = len(bf.get('batches', []))
                print(f'    - {name}: {bcount} batch(es)')
        sys.exit(0)
    elif tests_failed:
        print('Apply aborted: tests failed.')
        tr = r.get('test_results', {})
        failed = tr.get('failures', [])
        if failed:
            print(f'  Failed tests: {len(failed)}')
            for f in failed:
                print(f'    - {f}')
        sys.exit(4)
    elif not has_changes:
        print('No changes detected — nothing to apply.')
        sys.exit(0)
    else:
        print(f'Apply failed: {reason}')
        sys.exit(5)

# === Unknown / future endpoints: pretty-print JSON ===
else:
    print(json.dumps(r, indent=2))
    sys.exit(0)
"
    exit_code=$?

    # Translate backend error codes for agent readability
    case $exit_code in
        0)   ;;  # success / no changes
        2)   ;;  # HTTP / connection error (message already printed to stderr)
        3)   ;;  # JSON parse error
        4)   ;;  # tests failed
        *)   exit_code=1 ;;  # unknown failure
    esac

    return $exit_code
}

# ===================================================================
# Command handlers
# ===================================================================

# -------------------------------------------------------------------
# _handle_plan_command — intercepted plan preview via backend API.
# -------------------------------------------------------------------
_handle_plan_command() {
    local env_name explicit_env
    env_name=$(_resolve_env "$@")
    explicit_env=$(_extract_explicit_env "$@")

    # Warn (and override) when the LLM-supplied env disagrees with the
    # authoritative toolbar environment — guards against hallucinated targets.
    if [[ -n "${DATACODE_ENVIRONMENT:-}" && -n "$explicit_env" && "$explicit_env" != "$env_name" ]]; then
        echo "[sqlmesh.sh] WARNING: requested env '$explicit_env' overridden by active environment '$env_name'" >&2
    fi

    echo "[sqlmesh.sh] Intercepted 'plan $env_name' → backend API (POST /api/v1/plan)" >&2
    echo "[sqlmesh.sh] Workspace: $(_resolve_workspace_path)" >&2
    echo "[sqlmesh.sh] Environment: $env_name" >&2

    _export_api_env "$@"
    _invoke_backend_api "/api/v1/plan" "false"
}

# -------------------------------------------------------------------
# _handle_apply_command — intercepted apply via backend API.
# -------------------------------------------------------------------
_handle_apply_command() {
    local env_name explicit_env
    env_name=$(_resolve_env "$@")
    explicit_env=$(_extract_explicit_env "$@")

    # Warn (and override) when the LLM-supplied env disagrees with the
    # authoritative toolbar environment — guards against hallucinated targets.
    if [[ -n "${DATACODE_ENVIRONMENT:-}" && -n "$explicit_env" && "$explicit_env" != "$env_name" ]]; then
        echo "[sqlmesh.sh] WARNING: requested env '$explicit_env' overridden by active environment '$env_name'" >&2
    fi

    echo "[sqlmesh.sh] Intercepted 'apply $env_name' → backend API (POST /api/v1/apply)" >&2
    echo "[sqlmesh.sh] Workspace: $(_resolve_workspace_path)" >&2
    echo "[sqlmesh.sh] Environment: $env_name" >&2
    echo "[sqlmesh.sh] Trigger: $TRIGGER_TYPE, User: $USER_ID" >&2

    _export_api_env "$@"
    _invoke_backend_api "/api/v1/apply" "true"
}

# -------------------------------------------------------------------
# _handle_create_external_models_command — intercepted create_external_models
# via backend API POST /api/v1/external-models/import.
# -------------------------------------------------------------------
_handle_create_external_models_command() {
    local workspace_path
    workspace_path=$(_resolve_workspace_path)
    if [[ -z "$workspace_path" ]]; then
        echo "[sqlmesh.sh] ERROR: Cannot determine workspace_path." >&2
        echo "[sqlmesh.sh] Set DATACODE_WORKSPACE_PATH or run from within a sqlmesh project." >&2
        exit 1
    fi

    echo "[sqlmesh.sh] Intercepted 'create_external_models' → POST ${API_URL}/api/v1/external-models/import" >&2
    echo "[sqlmesh.sh] Workspace: ${workspace_path}" >&2

    # Parse CLI arguments (after 'create_external_models')
    local schema="" gateway="" include="" exclude="" samples="3" strict="false"
    local prev=""
    for arg in "$@"; do
        case "$prev" in
            --schema)   schema="$arg" ;;
            --gateway)  gateway="$arg" ;;
            --include)  include="$arg" ;;
            --exclude)  exclude="$arg" ;;
            --samples)  samples="$arg" ;;
        esac
        case "$arg" in
            --strict) strict="true" ;;
        esac
        prev="$arg"
    done

    if [[ -z "$schema" ]]; then
        echo "[sqlmesh.sh] ERROR: --schema is required for create_external_models." >&2
        echo "[sqlmesh.sh] Usage: sqlmesh create_external_models --schema <db> [--gateway <gw>] [--include <patterns>] [--exclude <patterns>] [--samples <N>] [--strict]" >&2
        exit 1
    fi

    echo "[sqlmesh.sh] Schema: ${schema}, Gateway: ${gateway:-(default)}, Strict: ${strict}" >&2

    # Export params as env vars, then reuse the shared HTTP transport.
    # Passing values via env (instead of interpolating into the python
    # source) avoids shell-injection through raw CLI arguments.
    export _AD_API_URL="$API_URL"
    export _AD_ENDPOINT="/api/v1/external-models/import"
    export _AD_WORKSPACE="$workspace_path"
    export _AD_EM_SCHEMA="$schema"
    export _AD_EM_GATEWAY="$gateway"
    export _AD_EM_INCLUDE="$include"
    export _AD_EM_EXCLUDE="$exclude"
    export _AD_EM_SAMPLES="$samples"
    export _AD_EM_STRICT="$strict"

    set +e
    python3 -c "
import json, os

# --- Build request payload from env; shared transport reads api_url/endpoint/request_body ---
api_url  = os.environ.get('_AD_API_URL', 'http://localhost:8002')
endpoint = os.environ.get('_AD_ENDPOINT', '/api/v1/external-models/import')

payload = {
    'workspace_path': os.environ.get('_AD_WORKSPACE', ''),
    'schema': os.environ.get('_AD_EM_SCHEMA', ''),
    'tables': [],
    'strict': os.environ.get('_AD_EM_STRICT', 'false') == 'true',
}
_gateway = os.environ.get('_AD_EM_GATEWAY', '')
_include = os.environ.get('_AD_EM_INCLUDE', '')
_exclude = os.environ.get('_AD_EM_EXCLUDE', '')
_samples = os.environ.get('_AD_EM_SAMPLES', '')
if _gateway:
    payload['gateway'] = _gateway
if _include:
    payload['include'] = _include
if _exclude:
    payload['exclude'] = _exclude
if _samples:
    payload['samples'] = int(_samples)

request_body = json.dumps(payload)
${_PY_TRANSPORT}
# --- Response handler: external models import ---
written = r.get('written_files', [])
skipped_count = r.get('skipped_count', 0)
success_count = r.get('success_count', 0)

print(f'External models import complete: {success_count} table(s) imported, {skipped_count} skipped.')
if written:
    print('Written files:')
    for f in written:
        print(f'  {f}')
"
    local exit_code=$?
    set -e

    if [[ $exit_code -ne 0 ]]; then
        echo "[sqlmesh.sh] create_external_models failed with exit code ${exit_code}" >&2
    fi
    return $exit_code
}

# ===================================================================
# Ontology command handlers
# ===================================================================

# -------------------------------------------------------------------
# _handle_ontology_command — intercepted ontology subcommands.
#
# Routes to the runtime's ontology API (default port 8002) via HTTP.
#
# Usage:
#   sqlmesh ontology validate <name>   Validate cross-references in a specific ontology
#   sqlmesh ontology validate --all    Validate all ontologies in the workspace
#   sqlmesh ontology list              List all available ontologies
#
# Exit codes:
#   0  success (valid or listed without error)
#   1  validation errors found
#   2  HTTP / connection error
#   3  invalid JSON response
# -------------------------------------------------------------------
_handle_ontology_command() {
    local subcommand="${1:-}"
    shift || true

    local workspace_path
    workspace_path=$(_resolve_workspace_path)
    if [[ -z "$workspace_path" ]]; then
        echo "[sqlmesh.sh] ERROR: Cannot determine workspace_path." >&2
        echo "[sqlmesh.sh] Set DATACODE_WORKSPACE_PATH or run from within a sqlmesh project." >&2
        exit 1
    fi

    case "$subcommand" in
        validate)
            local mode="validate_single"
            local ontology_name=""

            if _has_flag "--all" "$@"; then
                mode="validate_all"
            else
                # First non-flag positional arg is the ontology name
                for arg in "$@"; do
                    if [[ "$arg" != -* ]]; then
                        ontology_name="$arg"
                        break
                    fi
                done
                if [[ -z "$ontology_name" ]]; then
                    echo "[sqlmesh.sh] ERROR: ontology validate requires a name or --all." >&2
                    echo "[sqlmesh.sh] Usage:" >&2
                    echo "[sqlmesh.sh]   sqlmesh ontology validate <name>   Validate a specific ontology" >&2
                    echo "[sqlmesh.sh]   sqlmesh ontology validate --all    Validate all ontologies" >&2
                    exit 1
                fi
            fi

            echo "[sqlmesh.sh] Intercepted 'ontology validate' → Ontology Service (${ONTOLOGY_API_URL})" >&2
            echo "[sqlmesh.sh] Workspace: ${workspace_path}" >&2
            if [[ "$mode" == "validate_single" ]]; then
                echo "[sqlmesh.sh] Ontology: ${ontology_name}" >&2
            fi

            export _AD_ONTOLOGY_API_URL="$ONTOLOGY_API_URL"
            export _AD_WORKSPACE="$workspace_path"
            export _AD_ONTOLOGY_MODE="$mode"
            export _AD_ONTOLOGY_NAME="$ontology_name"
            _ontology_run_python
            ;;
        list)
            echo "[sqlmesh.sh] Intercepted 'ontology list' → Ontology Service (${ONTOLOGY_API_URL})" >&2
            echo "[sqlmesh.sh] Workspace: ${workspace_path}" >&2

            export _AD_ONTOLOGY_API_URL="$ONTOLOGY_API_URL"
            export _AD_WORKSPACE="$workspace_path"
            export _AD_ONTOLOGY_MODE="list"
            export _AD_ONTOLOGY_NAME=""
            _ontology_run_python
            ;;
        ""|--help|-h)
            echo "Usage: sqlmesh ontology <subcommand> [options]" >&2
            echo "" >&2
            echo "Subcommands:" >&2
            echo "  validate <name>   Validate cross-references in a specific ontology" >&2
            echo "  validate --all    Validate all ontologies in the workspace" >&2
            echo "  list              List all available ontologies" >&2
            echo "" >&2
            echo "Environment:" >&2
            echo "  DATACODE_ONTOLOGY_SERVICE_URL  Ontology service URL (default: http://localhost:8002)" >&2
            exit 0
            ;;
        *)
            echo "[sqlmesh.sh] ERROR: Unknown ontology subcommand: '$subcommand'" >&2
            echo "[sqlmesh.sh] Run 'sqlmesh ontology --help' for usage." >&2
            exit 1
            ;;
    esac
}

# -------------------------------------------------------------------
# _ontology_run_python — inline Python HTTP client for the Ontology
# Service.
#
# Reads mode from _AD_ONTOLOGY_MODE:
#   list             → GET  /api/v1/ontology
#   validate_single  → POST /api/v1/ontology/{name}/validate
#   validate_all     → list + validate each
#
# Exit codes: 0 = ok, 1 = validation errors, 2 = HTTP error, 3 = bad JSON
# -------------------------------------------------------------------
_ontology_run_python() {
    set +e
    python3 -c "
import json, os, sys, urllib.request, urllib.error, urllib.parse

api_url   = os.environ.get('_AD_ONTOLOGY_API_URL', 'http://localhost:8002')
mode      = os.environ.get('_AD_ONTOLOGY_MODE', '')
workspace = os.environ.get('_AD_WORKSPACE', '')
name      = os.environ.get('_AD_ONTOLOGY_NAME', '')

ws_param = urllib.parse.urlencode({'workspace_path': workspace})


def _http(method, url):
    try:
        data = b'' if method == 'POST' else None
        req = urllib.request.Request(url, data=data, method=method)
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8') if e.fp else '{}'
        try:
            detail = json.loads(body).get('detail', body)
        except Exception:
            detail = body
        if isinstance(detail, dict):
            # Print the top-level reason first, then each structured schema
            # error (location + message + rejected value) so the agent/user
            # knows exactly which fields to fix.
            summary = detail.get('error') or detail.get('code') or json.dumps(detail)
            print(f'[sqlmesh.sh] ERROR: Backend returned HTTP {e.code}: {summary}', file=sys.stderr)
            field_errors = detail.get('errors')
            if isinstance(field_errors, list) and field_errors:
                print(f'[sqlmesh.sh] Details ({len(field_errors)} issue(s)):', file=sys.stderr)
                for fe in field_errors:
                    if not isinstance(fe, dict):
                        print(f'[sqlmesh.sh]   - {fe}', file=sys.stderr)
                        continue
                    loc = fe.get('location', '?')
                    fmsg = fe.get('message', '')
                    inp = fe.get('input')
                    line = f'  - {loc}: {fmsg}'
                    if inp is not None:
                        line += f' (got: {inp!r})'
                    print(f'[sqlmesh.sh] {line}', file=sys.stderr)
        else:
            print(f'[sqlmesh.sh] ERROR: Backend returned HTTP {e.code}: {detail}', file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(f'[sqlmesh.sh] ERROR: Cannot reach ontology service at {api_url}', file=sys.stderr)
        print(f'[sqlmesh.sh] {e.reason}', file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f'[sqlmesh.sh] ERROR: {e}', file=sys.stderr)
        sys.exit(3)


def _print_errors(errors):
    for err in errors:
        path = err.get('path', '?')
        msg  = err.get('message', '')
        sev  = err.get('severity', 'error')
        print(f'  [{sev}] {path}: {msg}')


# === Mode: list ===
if mode == 'list':
    url = f'{api_url}/api/v1/ontology?{ws_param}'
    models = _http('GET', url)
    if not models:
        print('No ontologies found in workspace.')
        sys.exit(0)
    print('Available ontologies (%d):' % len(models))
    print()
    print('  %-30s Description' % 'Name')
    print('  %s %s' % ('-' * 30, '-' * 11))
    for m in models:
        n = m.get('name', '?')
        d = m.get('description') or ''
        print('  %-30s %s' % (n, d))
    sys.exit(0)

# === Mode: validate_single ===
elif mode == 'validate_single':
    url = f'{api_url}/api/v1/ontology/{urllib.parse.quote(name)}/validate?{ws_param}'
    result = _http('POST', url)
    oname  = result.get('ontology_name', name)
    valid  = result.get('valid', False)
    errors = result.get('errors', [])
    if valid:
        print(f'Ontology \\\"{oname}\\\" is VALID. No validation errors found.')
        sys.exit(0)
    else:
        print(f'Ontology \\\"{oname}\\\" is INVALID ({len(errors)} error(s)):')
        print()
        _print_errors(errors)
        sys.exit(1)

# === Mode: validate_all ===
elif mode == 'validate_all':
    list_url = f'{api_url}/api/v1/ontology?{ws_param}'
    models = _http('GET', list_url)
    if not models:
        print('No ontologies found in workspace.')
        sys.exit(0)
    total = len(models)
    print(f'Validating {total} ontology(ies)...')
    print()
    valid_count = 0
    invalid_count = 0
    for i, m in enumerate(models, 1):
        oname = m.get('name', '?')
        val_url = f'{api_url}/api/v1/ontology/{urllib.parse.quote(oname)}/validate?{ws_param}'
        result = _http('POST', val_url)
        valid  = result.get('valid', False)
        errors = result.get('errors', [])
        if valid:
            print(f'[{i}/{total}] {oname} ... VALID')
            valid_count += 1
        else:
            print(f'[{i}/{total}] {oname} ... INVALID ({len(errors)} error(s))')
            _print_errors(errors)
            print()
            invalid_count += 1
    print()
    print(f'Summary: {valid_count} valid, {invalid_count} invalid, {total} total.')
    sys.exit(1 if invalid_count > 0 else 0)

else:
    print(f'[sqlmesh.sh] ERROR: Unknown ontology mode: {mode}', file=sys.stderr)
    sys.exit(1)
"
    local exit_code=$?
    set -e
    return $exit_code
}

# ===================================================================
# Main dispatch logic
# ===================================================================
SUBCOMMAND="${1:-}"

# Reject the legacy/destructive --auto-apply on plan.
if [[ "$SUBCOMMAND" == "plan" ]] && _has_flag "--auto-apply" "$@"; then
    echo "[sqlmesh.sh] ERROR: 'plan --auto-apply' is disabled on this platform." >&2
    echo "[sqlmesh.sh] Preview and deploy are separate commands:" >&2
    echo "[sqlmesh.sh]   1) Preview : sqlmesh plan <env>" >&2
    echo "[sqlmesh.sh]   2) Deploy  : sqlmesh apply <env>   (only after user confirmation)" >&2
    exit 1
fi

# plan preview → backend API (replaces the old passthrough to native sqlmesh)
if [[ "$SUBCOMMAND" == "plan" ]]; then
    _handle_plan_command "$@"
    exit $?
fi

# Dedicated deploy command → backend API
if [[ "$SUBCOMMAND" == "apply" ]]; then
    _handle_apply_command "$@"
    exit $?
fi

# create_external_models → backend API POST /api/v1/external-models/import
if [[ "$SUBCOMMAND" == "create_external_models" ]]; then
    _handle_create_external_models_command "$@"
    exit $?
fi

# ontology validate/list → the runtime's ontology API (port 8002)
if [[ "$SUBCOMMAND" == "ontology" ]]; then
    _handle_ontology_command "${@:2}"
    exit $?
fi

# run → REJECTED. Data refresh is a write operation, and the native binary
# ignores DATACODE_ENVIRONMENT — it would bypass the environment
# guardrail, the environment lock, and the audit trail.
if [[ "$SUBCOMMAND" == "run" ]]; then
    echo "[sqlmesh.sh] ERROR: 'sqlmesh run' is disabled on this platform." >&2
    echo "[sqlmesh.sh] 'run' refreshes data. The native binary does not read" >&2
    echo "[sqlmesh.sh] DATACODE_ENVIRONMENT, so it would bypass the environment" >&2
    echo "[sqlmesh.sh] guardrail, the environment lock, and auditing. Use instead:" >&2
    echo "[sqlmesh.sh]   1) sqlmesh plan / sqlmesh apply <env>   # versioned data landing" >&2
    echo "[sqlmesh.sh]   2) dc run <job_name>                    # orchestrated refresh via Dagster" >&2
    exit 1
fi

# Default: passthrough to real sqlmesh, auto-located to the SQLMesh project
# directory (the shell tool runs us from the worktree root, but native
# sqlmesh requires cwd where config.yaml lives).
_PROJECT_DIR=$(_resolve_sqlmesh_project_dir)
echo "[sqlmesh.sh] Passthrough → native sqlmesh: sqlmesh $*" >&2
if [[ "$_PROJECT_DIR" != "$PWD" ]]; then
    echo "[sqlmesh.sh] Project directory → $_PROJECT_DIR" >&2
    cd "$_PROJECT_DIR"
fi
exec "$REAL_SQLMESH" "$@"
