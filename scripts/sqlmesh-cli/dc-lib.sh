#!/usr/bin/env bash
# ============================================================================
# dc-lib.sh — shared library for the dc platform CLI (dc = DataCursor).
# ============================================================================
# Sourced by dc.sh ONLY. Provides:
#   _dc_resolve_workspace_path  — workspace (worktree) root resolution
#   _dc_resolve_env             — authoritative active environment
#   _dc_has_flag                — CLI flag membership test
#   _dc_require_workspace       — resolve workspace or fail with guidance
#   _DC_PY_TRANSPORT            — shared inline-Python HTTP transport snippet
#
# Parameter injection safety: CLI arguments and paths NEVER get interpolated
# into Python source. They cross the bash→Python boundary exclusively via
# _DC_* environment variables (same convention as _AD_* in sqlmesh.sh).
# ============================================================================

# -------------------------------------------------------------------
# Resolve the workspace (worktree) root.
#
# Priority:
#   1. AGENTICDATA_WORKSPACE_PATH — injected by the agent shell tool.
#   2. Walk up from cwd to the directory whose sql-warehouse/ sub-directory
#      holds the SQLMesh config (platform dual-directory layout).
#   3. Walk up to a directory containing config.yaml (bare SQLMesh project).
# Returns an empty string when nothing matches.
# -------------------------------------------------------------------
_dc_resolve_workspace_path() {
    if [[ -n "${AGENTICDATA_WORKSPACE_PATH:-}" ]]; then
        echo "${AGENTICDATA_WORKSPACE_PATH}"
        return
    fi
    local d="$PWD"
    while [[ "$d" != "/" ]]; do
        if [[ -f "$d/sql-warehouse/config.yaml" ]]; then
            echo "$d"
            return
        fi
        d="$(dirname "$d")"
    done
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
# Resolve the AUTHORITATIVE active environment.
#
# AGENTICDATA_ENVIRONMENT comes from the toolbar selection via the agent
# shell tool. Unlike sqlmesh.sh's _resolve_env there is no CLI positional
# fallback: dc commands take no environment argument — the working copy
# (workspace_path) is authoritative on the backend, and the environment
# value is only sent as a CONFIRMATION (a mismatch is rejected). When the
# variable is unset (direct human CLI usage), nothing is confirmed and the
# backend derives the environment from the working copy.
# -------------------------------------------------------------------
_dc_resolve_env() {
    echo "${AGENTICDATA_ENVIRONMENT:-}"
}

# -------------------------------------------------------------------
# Check if an argument list contains a specific flag
# -------------------------------------------------------------------
_dc_has_flag() {
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
# Resolve the workspace path or fail with actionable guidance
# -------------------------------------------------------------------
_dc_require_workspace() {
    local workspace_path
    workspace_path=$(_dc_resolve_workspace_path)
    if [[ -z "$workspace_path" ]]; then
        echo "[dc] ERROR: Cannot determine workspace_path." >&2
        echo "[dc] Set AGENTICDATA_WORKSPACE_PATH or run from within a workspace." >&2
        exit 1
    fi
    echo "$workspace_path"
}

# -------------------------------------------------------------------
# _DC_PY_TRANSPORT — shared Python snippet: perform the HTTP request
# described by the python variables `method`, `url` and `request_body`
# (empty string for GET) and parse the JSON response into `r`.
#
# Callers embed this snippet after defining those variables and consume
# `r` afterwards. Standard exit codes: 2 = HTTP/connection error,
# 3 = invalid JSON.
#
# NOTE: this string is interpolated inside a double-quoted `python3 -c`
# argument — single quotes only, and no `$` characters.
# -------------------------------------------------------------------
_DC_PY_TRANSPORT="
import json, sys, urllib.request, urllib.error

try:
    _data = request_body.encode('utf-8') if request_body else None
    _req = urllib.request.Request(
        url,
        data=_data,
        headers={'Content-Type': 'application/json'} if _data else {},
        method=method,
    )
    with urllib.request.urlopen(_req, timeout=300) as _resp:
        _response_body = _resp.read().decode('utf-8')
except urllib.error.HTTPError as e:
    _body = e.read().decode('utf-8') if e.fp else '{}'
    print(f'[dc] ERROR: Backend returned HTTP {e.code}', file=sys.stderr)
    try:
        _err = json.loads(_body)
    except Exception:
        _err = None
    if isinstance(_err, dict):
        # Print every distinct human-readable chunk the backend provides, in
        # priority order, so the actual cause is ALWAYS shown (same convention
        # as sqlmesh.sh):
        #   - report:         rich native-CLI-style diagnostics
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
            if _val not in _chunks:
                _chunks.append(_val)
        if not _chunks:
            _chunks = [_body]
        for _chunk in _chunks:
            for _line in (_chunk.splitlines() or ['']):
                print(f'[dc] {_line}', file=sys.stderr)
    else:
        print(f'[dc] {_body}', file=sys.stderr)
    sys.exit(2)
except urllib.error.URLError as e:
    print(f'[dc] ERROR: Cannot reach backend API at {url}', file=sys.stderr)
    print(f'[dc] {e.reason}', file=sys.stderr)
    sys.exit(2)
except Exception as e:
    print(f'[dc] ERROR: {e}', file=sys.stderr)
    sys.exit(3)

try:
    r = json.loads(_response_body)
except Exception:
    print('[dc] ERROR: Invalid JSON response from backend', file=sys.stderr)
    sys.exit(3)
"
