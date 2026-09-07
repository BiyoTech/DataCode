"""SqlMeshExecutor wire protocol — FROZEN at P0 (2026-08-26), do not change.

This is the contract between workspace-runtime and the isolated executor that
holds the EXECUTABLE SQLMesh Context (design 07 ADR-005 / Q1 decision).

Why it must be frozen:
    P3 ships the executor as an L3-proc persistent child process. P6 replaces
    that child process with an L3-base container (per-lease warm sandbox).
    The PROTOCOL IS IDENTICAL in both forms — swapping the execution vehicle
    must not touch the caller. A protocol change after P3 therefore means
    changing both sides simultaneously and is treated as a breaking contract
    bump (bump PROTOCOL_VERSION and add a migration note at the bottom).

Transport:
    UNIX domain socket, one connection per runtime -> executor pair.
    Frames: 4-byte big-endian payload length, then that many bytes of UTF-8
    JSON. One request in flight per connection at a time (the runtime may
    open additional connections for concurrent ops against the same working
    copy; the executor serializes writes internally).

    Length prefix is capped at MAX_FRAME_BYTES; larger payloads are a
    protocol violation and the connection is dropped (the executor restarts
    cleanly — it holds no platform credentials, so a restart is cheap).

Message shapes:
    Request:  {"v": 1, "id": <int>, "op": <str>, "params": {...}}
    Response: {"v": 1, "id": <int>, "ok": true,  "result": {...}}
              {"v": 1, "id": <int>, "ok": false, "error": {"code": ..., "message": ...}}
    Event:    {"v": 1, "ev": <str>, "data": {...}}   (executor -> runtime, async)

Error codes are stable strings (not Python exceptions) so the container form
(P6, possibly a different image) can emit the same set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datacode_common.framing import (
    ProtocolError,
    encode,
)
from datacode_common.framing import read_frame as _read_frame

# Bump ONLY with a simultaneous runtime+executor release and a note in the
# migration log at the bottom of this file.
PROTOCOL_VERSION = 1

# ── Operations (request "op") ──────────────────────────────────────────────
# Lifecycle
OP_PING = "ping"                        # params {} -> result {"alive": true}
OP_SHUTDOWN = "shutdown"                # graceful stop after in-flight op

# Context lifecycle (the executor holds ONE working copy)
OP_CONTEXT_LOAD = "context.load"        # params {workspace_path, gateway?, config_overrides?}
                                        # -> result ContextMetadataDTO as dict
OP_CONTEXT_REFRESH = "context.refresh"  # re-scan after file writes
OP_CONTEXT_INVALIDATE = "context.invalidate"  # drop the in-memory Context

# Read-side metadata (feeds runtime's MetadataContext; results are pure data,
# no code objects may cross this boundary)
OP_METADATA_MODELS = "metadata.models"
OP_METADATA_LINEAGE = "metadata.lineage"            # params {model?: str}
OP_METADATA_COLUMNS = "metadata.columns"            # params {model: str}
OP_METADATA_ENVIRONMENTS = "metadata.environments"  # engine-side env view
OP_METADATA_STATE_FINGERPRINT = "metadata.state_fingerprint"
OP_METADATA_METRICS = "metadata.metrics"            # project METRIC names

# Execute-side (the ops that made this executor necessary: they exec user
# Python models/macros, which is exactly what must NOT happen in-process)
OP_PLAN = "sqlmesh.plan"                # params PlanParams -> PlanResultDTO
OP_RENDER = "sqlmesh.render"            # params {model} -> {sql}
OP_EVALUATE = "sqlmesh.evaluate"        # params EvaluateParams -> EvaluateResult
OP_TEST = "sqlmesh.test"                # -> TestResult
OP_QUERY = "sqlmesh.query"              # params {sql, row_limit} -> QueryResult
OP_FORMAT = "sqlmesh.format"            # params {sql} -> {sql_formatted}
OP_TABLE_DIFF = "sqlmesh.table_diff"    # params {source, target, on, ...} -> diff

# ── Ops deliberately NOT here ─────────────────────────────────────────────
#
# ``sqlmesh.apply``: writes were removed from this protocol (P6c). The warm
# sandbox holds at most a READ-ONLY warehouse account, so an apply op here
# could only ever fail — or, worse, be wired to a writable one by a future
# caller. The write half runs as a task (task kind "sqlmesh.apply") through the
# task plane, where the L2 profile is the one place allowed a writable account.
#
# ``shell.exec``: agent shell moved to the task plane as well (task kind
# "agent.shell"), so both user-code vehicles share one submission contract and
# one set of isolation profiles. Nothing calls the op any more.
#
# Both names survive in the TASK plane with the same strings; that is a
# different namespace (TaskSpec.kind), not this one.

# ── Events (executor -> runtime, unsolicited) ─────────────────────────────
EV_LOG = "log"                          # data {"level", "message", "ts"}
EV_PROGRESS = "progress"                # data {"stage", "percent?", "detail?"}
EV_HEARTBEAT = "heartbeat"              # data {"rss_bytes", "uptime_s"}

# ── Stable error codes ────────────────────────────────────────────────────
ERR_UNKNOWN_OP = "unknown_op"
ERR_BAD_PARAMS = "bad_params"
ERR_CONTEXT_NOT_LOADED = "context_not_loaded"
ERR_CONTEXT_CONFIG = "context_config_error"
ERR_CONTEXT_LOAD = "context_load_error"
ERR_SQLMESH_PLAN = "plan_failed"
ERR_SQLMESH_APPLY = "apply_failed"
ERR_SQLMESH_EVALUATE = "evaluate_failed"
ERR_TIMEOUT = "operation_timeout"
ERR_RESOURCE_LIMIT = "resource_limit"   # rlimit hit (RLIMIT_AS/CPU/NPROC/...)
ERR_CRASHED = "executor_crashed"        # synthesized by the runtime, never sent


# ── Serializable result envelopes ──────────────────────────────────────────
# Pure-data contracts. Anything not expressible here (SQLGlot AST nodes,
# engine connections, live Python objects) must be reduced BEFORE crossing
# the socket — that reduction is the whole point of the metadata split.


@dataclass(frozen=True)
class ModelSummaryDTO:
    """One model as seen by the read-side MetadataContext."""

    name: str
    kind: str                      # "model" | "seed" | "external" | ...
    sql_or_python: str = ""        # rendered SQL (python models: rendered output)
    depends_on: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)  # name -> type
    tags: list[str] = field(default_factory=list)
    path: str = ""


@dataclass(frozen=True)
class ContextMetadataDTO:
    """Full metadata payload returned by OP_METADATA_* and context.load.

    This is what the runtime keeps hot in its MetadataContext: everything the
    /rt/v1 read endpoints need, none of it executable.
    """

    workspace_path: str
    gateway: str | None = None
    models: list[ModelSummaryDTO] = field(default_factory=list)
    environments: list[dict[str, Any]] = field(default_factory=list)
    state_fingerprint: str = ""    # opaque; runtime compares to detect drift
    dialect: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the wire (dataclasses.asdict equivalent, explicit)."""
        return {
            "workspace_path": self.workspace_path,
            "gateway": self.gateway,
            "models": [m.__dict__ for m in self.models],
            "environments": self.environments,
            "state_fingerprint": self.state_fingerprint,
            "dialect": self.dialect,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextMetadataDTO":
        models = [ModelSummaryDTO(**m) for m in data.get("models", [])]
        return cls(
            workspace_path=data["workspace_path"],
            gateway=data.get("gateway"),
            models=models,
            environments=list(data.get("environments", [])),
            state_fingerprint=data.get("state_fingerprint", ""),
            dialect=data.get("dialect", ""),
        )


@dataclass(frozen=True)
class PlanResultDTO:
    """Pure-data plan summary (the human diff stays a string)."""

    environment: str
    direct: list[str] = field(default_factory=list)
    indirect: list[str] = field(default_factory=list)
    metadata_changed: list[str] = field(default_factory=list)
    breaking: list[str] = field(default_factory=list)
    forward_only: bool = False
    affected_rows: int | None = None
    plan_id: str = ""              # identifies the plan across preview -> apply
    diff_summary: str = ""


# Shell results are NOT a DTO here: shell is a task now (kind "agent.shell"),
# and task results travel as the task protocol's result dict. Keeping a typed
# shell DTO in the executor protocol would describe an op that no longer exists.


# ── Frame codec ────────────────────────────────────────────────────────────
#
# The framing itself lives in datacode_common.framing, shared with
# warm_protocol: two long-lived socket channels, one wire format, one
# implementation. Re-exported here so existing importers keep working and the
# protocol module still reads as self-contained.


def encode_message(message: dict[str, Any]) -> bytes:
    """Frame one JSON message: 4-byte BE length + UTF-8 JSON payload."""
    return encode(message)


async def read_message(reader) -> dict[str, Any]:
    """Read and decode one framed message from an async stream.

    ``reader`` must have asyncio StreamReader's read() semantics. Returns {}
    on clean EOF (peer closed between frames).

    Adds the version check on top of the shared codec: a frame from a peer
    speaking another version must be refused HERE, because everything below
    this line assumes the envelope shape of :data:`PROTOCOL_VERSION`.
    """
    message = await _read_frame(reader)
    if not message:
        return {}
    if message.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol message: {message.get('v')!r}")
    return message


def make_request(req_id: int, op: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a well-formed request envelope."""
    return {"v": PROTOCOL_VERSION, "id": req_id, "op": op, "params": params or {}}


def make_response(req_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": req_id, "ok": True, "result": result}


def make_error(req_id: int, code: str, message: str) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": req_id, "ok": False,
            "error": {"code": code, "message": message}}


# ── Migration log (append on PROTOCOL_VERSION bump) ───────────────────────
# v1 (2026-08-26, P0): initial freeze. Ops: lifecycle + context + metadata +
#    plan/apply/render/evaluate/test/query/format + shell.exec. Events: log,
#    progress, heartbeat.
# v1 (2026-08-28, P6 close-out): ADDITIVE ops only, PROTOCOL_VERSION unchanged.
#    Added metadata.metrics and sqlmesh.table_diff (both implemented when the
#    read-side services moved into the sandbox). Additive op registration is
#    safe without a version bump because runtime and executor are built and
#    deployed from the same tree — there is no cross-version deployment to
#    protect, and an unknown op already answers a stable error. Every other
#    capability of this close-out (model detail via metadata.columns, column
#    lineage via metadata.lineage params, detailed tests, render options)
#    evolved through PARAMS of already-frozen ops, which the freeze explicitly
#    permits. Frames unchanged.
