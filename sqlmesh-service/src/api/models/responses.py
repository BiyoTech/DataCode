"""Pydantic response models for REST API endpoints.

Matches the SQLMesh web server response structure for compatibility,
adapted for our multi-tenant Context Pool architecture.
"""

from __future__ import annotations

import enum
import typing as t

from pydantic import BaseModel, Field


# --- Enums ---

class ModelType(str, enum.Enum):
    PYTHON = "python"
    SQL = "sql"
    SEED = "seed"
    EXTERNAL = "external"
    SOURCE = "source"


class NodeType(str, enum.Enum):
    MODEL = "model"
    SOURCE = "source"


class SnapshotChangeCategoryType(str, enum.Enum):
    BREAKING = "breaking"
    NON_BREAKING = "non_breaking"
    INDIRECT_BREAKING = "indirect_breaking"
    INDIRECT_NON_BREAKING = "indirect_non_breaking"
    METADATA = "metadata"


class PlanStageName(str, enum.Enum):
    VALIDATION = "validation"
    CHANGES = "changes"
    BACKFILLS = "backfills"
    CREATION = "creation"
    RESTATE = "restate"
    BACKFILL = "backfill"
    PROMOTE = "promote"
    CANCEL = "cancel"


class StageStatus(str, enum.Enum):
    INIT = "init"
    SUCCESS = "success"
    FAIL = "fail"


# --- Model Responses ---

class Column(BaseModel):
    name: str
    type: str
    description: t.Optional[str] = None


class Reference(BaseModel):
    name: str
    expression: str
    unique: bool


class ModelDetails(BaseModel):
    owner: t.Optional[str] = None
    kind: t.Optional[str] = None
    batch_size: t.Optional[int] = None
    cron: t.Optional[str] = None
    stamp: t.Optional[str] = None
    start: t.Optional[str] = None
    retention: t.Optional[int] = None
    table_format: t.Optional[str] = None
    storage_format: t.Optional[str] = None
    time_column: t.Optional[str] = None
    tags: t.Optional[str] = None
    references: t.List[Reference] = []
    partitioned_by: t.Optional[str] = None
    clustered_by: t.Optional[str] = None
    lookback: t.Optional[int] = None
    cron_prev: t.Optional[str] = None
    cron_next: t.Optional[str] = None
    interval_unit: t.Optional[str] = None
    annotated: t.Optional[bool] = None


class ModelResponse(BaseModel):
    name: str
    fqn: str
    path: t.Optional[str] = None
    full_path: t.Optional[str] = None
    dialect: str = ""
    type: ModelType = ModelType.SQL
    columns: t.List[Column] = []
    description: t.Optional[str] = None
    details: t.Optional[ModelDetails] = None
    sql: t.Optional[str] = None
    default_catalog: t.Optional[str] = None
    hash: str = ""
    # Gateway the model is bound to. Populated for external models (imported
    # from a specific gateway) and optionally for SQL/Python models that
    # declare one. None means "use the default gateway".
    gateway: t.Optional[str] = None


class ModelListItem(BaseModel):
    """Lightweight model info for list endpoints."""
    name: str
    fqn: str
    path: t.Optional[str] = None
    dialect: str = ""
    type: ModelType = ModelType.SQL
    kind: t.Optional[str] = None
    description: t.Optional[str] = None
    owner: t.Optional[str] = None
    tags: t.List[str] = []
    cron: t.Optional[str] = None
    # Gateway the model is bound to (especially relevant for external models
    # imported from a specific gateway). None for models without a gateway.
    gateway: t.Optional[str] = None


class ModelListResponse(BaseModel):
    models: t.List[ModelListItem]


# --- Plan Responses ---

class ChangeDisplay(BaseModel):
    name: str
    view_name: str = ""
    node_type: NodeType = NodeType.MODEL
    parents: t.Set[str] = set()


class ChangeDirect(ChangeDisplay):
    diff: str = ""
    indirect: t.List[ChangeDisplay] = []
    direct: t.List[ChangeDisplay] = []
    change_category: t.Optional[str] = None


class ChangeIndirect(ChangeDisplay):
    pass


class ModelsDiff(BaseModel):
    direct: t.List[ChangeDirect] = []
    indirect: t.List[ChangeIndirect] = []
    metadata: t.List[ChangeDisplay] = []


class PlanChanges(BaseModel):
    added: t.List[ChangeDisplay] = []
    removed: t.List[ChangeDisplay] = []
    modified: t.Optional[ModelsDiff] = None


class BackfillDetails(ChangeDisplay):
    interval: t.List[str] = []
    batches: int = 0


class PlanTestFailureDetail(BaseModel):
    """Single failed/errored test case with its formatted traceback."""
    message: str = ""
    details: str = ""


class PlanTestResults(BaseModel):
    """Structured unit test results surfaced when a plan's tests fail.

    Mirrors the SQLMesh web server's ReportTestsFailure structure so the
    frontend can render a rich "One or More Tests Failed" panel.
    """
    total: int = 0
    failures: int = 0
    errors: int = 0
    successful: int = 0
    dialect: str = ""
    details: t.List[PlanTestFailureDetail] = []
    traceback: str = ""


class PlanLintViolation(BaseModel):
    """Single linter violation attached to a model file.

    Mirrors the SQLMesh CLI's per-file lint output
    (e.g. ``ambiguousorinvalidcolumn: Unknown column: hidden for model '...', ...``)
    so the frontend can render actionable, file-level diagnostics instead of
    a generic 500.
    """
    model_name: str = ""
    file_path: str = ""
    rule: str = ""
    message: str = ""
    severity: str = "error"
    line: t.Optional[int] = None
    column: t.Optional[int] = None


class PlanLintResults(BaseModel):
    """All lint violations grouped by file, for the plan UI's lint panel."""
    total: int = 0
    errors: int = 0
    warnings: int = 0
    by_file: t.List[PlanLintViolation] = []
    # Native-CLI-style grouped text (``Linter errors for <file>:\n - rule: msg``)
    # so clients can display the exact output a local ``sqlmesh plan`` produces.
    report: str = ""
    message: str = ""


class PlanResult(BaseModel):
    """Full plan result with changes, backfills, and state fingerprint."""
    environment: str = "dev"
    has_changes: bool = False
    requires_backfill: bool = False
    changes: t.Optional[PlanChanges] = None
    backfills: t.Optional[t.List[BackfillDetails]] = None
    state_fingerprint: t.Optional[str] = None
    start: t.Optional[str] = None
    end: t.Optional[str] = None
    # Test execution status: tells the frontend whether tests were skipped,
    # passed, or failed (with structured failure details).
    tests_skipped: bool = False
    tests_passed: bool = False
    tests_failed: bool = False
    test_results: t.Optional[PlanTestResults] = None
    # Lint execution status: populated when the plan is rejected by the linter
    # so the UI can render per-file, per-rule diagnostics instead of a 500.
    lint_failed: bool = False
    lint_results: t.Optional[PlanLintResults] = None


class PlanApplyResult(BaseModel):
    """Result of plan apply operation."""
    environment: str = "dev"
    applied: bool = False
    reason: t.Optional[str] = None
    has_changes: bool = False
    requires_backfill: bool = False


# --- Environment Responses ---

class EnvironmentSummary(BaseModel):
    name: str
    snapshots: t.List[str] = []
    start_at: str = ""
    plan_id: str = ""
    expiration_timestamp: t.Optional[int] = None
    finalized_ts: t.Optional[int] = None


class EnvironmentsResponse(BaseModel):
    environments: t.Dict[str, EnvironmentSummary] = {}
    pinned_environments: t.Set[str] = set()
    default_target_environment: str = ""


# --- Lineage Responses ---

class LineageNodeColumn(BaseModel):
    """A single column in a lineage node card."""
    name: str
    type: str


class LineageNode(BaseModel):
    """Metadata for a single node in the lineage graph, used for card rendering."""
    name: str
    fqn: str
    type: str = "sql"
    kind: str = ""
    columns: t.List[LineageNodeColumn] = []


class LineageColumn(BaseModel):
    source: t.Optional[str] = None
    expression: t.Optional[str] = None
    models: t.Dict[str, t.Set[str]] = {}


class ModelLineageResponse(BaseModel):
    """Model-level lineage (parent/child graph) with optional node metadata."""
    graph: t.Dict[str, t.List[str]]
    nodes: t.Dict[str, LineageNode] = {}


class ColumnLineageResponse(BaseModel):
    """Column-level lineage adjacency list."""
    lineage: t.Dict[str, t.Dict[str, LineageColumn]]


# --- Query / Evaluate Responses ---

class QueryResult(BaseModel):
    columns: t.List[str]
    rows: t.List[t.List[t.Any]]
    total_rows: int
    execution_time_ms: int


class RenderedQuery(BaseModel):
    sql: str


class EvaluateResult(BaseModel):
    columns: t.List[str]
    rows: t.List[t.List[t.Any]]
    total_rows: int
    execution_time_ms: int


# --- Schema Discovery ---

class ColumnInfo(BaseModel):
    name: str
    data_type: str


class TableInfo(BaseModel):
    schema_name: str  # Renamed from "schema" to avoid shadowing BaseModel attribute
    table_name: str
    columns: t.List[ColumnInfo] = []


class SchemaDiscoveryResult(BaseModel):
    tables: t.List[TableInfo] = []


# --- External Models Import ---

class GatewayInfo(BaseModel):
    name: str
    is_default: bool
    dialect: str = ""  # e.g. "postgres", "mysql", "duckdb" — drives frontend cascade depth


class GatewaysResponse(BaseModel):
    gateways: t.List[GatewayInfo]
    default_gateway: str


class SchemaInfo(BaseModel):
    name: str


class SchemasResponse(BaseModel):
    schemas: t.List[SchemaInfo]


class ImportExternalModelsResult(BaseModel):
    success: bool
    written_files: t.List[str] = []
    skipped_count: int = 0
    success_count: int = 0
    error: t.Optional[str] = None


# --- Table Diff ---

class SchemaDiff(BaseModel):
    source: str = ""
    target: str = ""
    source_schema: t.Dict[str, str] = {}
    target_schema: t.Dict[str, str] = {}
    added: t.Dict[str, str] = {}
    removed: t.Dict[str, str] = {}
    modified: t.Dict[str, str] = {}


class RowDiffSummary(BaseModel):
    source_count: int = 0
    target_count: int = 0
    count_pct_change: float = 0.0
    stats: t.Dict[str, float] = {}


class TableDiffResult(BaseModel):
    schema_diff: SchemaDiff
    row_diff: RowDiffSummary
    on: t.List[t.List[str]] = []


# --- Test ---

class TestCaseResult(BaseModel):
    name: str
    path: t.Optional[str] = None


class TestErrorOrFailure(TestCaseResult):
    tb: str = ""


class TestSkipped(TestCaseResult):
    reason: str = ""


class TestResult(BaseModel):
    tests_run: int = 0
    errors: t.List[TestErrorOrFailure] = []
    failures: t.List[TestErrorOrFailure] = []
    skipped: t.List[TestSkipped] = []
    successes: t.List[TestCaseResult] = []


# --- Validation ---

class ValidationResult(BaseModel):
    valid: bool
    errors: t.List[str] = []
    warnings: t.List[str] = []


# --- SSE Events ---

class SSEEvent(BaseModel):
    """Generic SSE event payload."""
    event: str
    data: t.Any = None


# --- Apply History ---

class ApplyHistoryRecord(BaseModel):
    """Single apply history row returned in list endpoint (summary-only).

    plan_result is excluded from list responses to keep payloads lean;
    fetch the detail endpoint to get full changes/backfills/test_results.
    """
    id: str
    workspace_path: str
    environment: str
    user_id: str = ""
    session_id: str = ""
    trigger_type: str = "human"
    applied: bool = False
    reason: t.Optional[str] = None
    has_changes: bool = False
    added_count: int = 0
    removed_count: int = 0
    direct_count: int = 0
    indirect_count: int = 0
    metadata_count: int = 0
    backfill_count: int = 0
    duration_ms: int = 0
    error_message: t.Optional[str] = None
    created_at: str = ""


class ApplyHistoryDetail(BaseModel):
    """Full apply history record with complete plan result (for detail view)."""
    id: str
    workspace_path: str
    environment: str
    user_id: str = ""
    session_id: str = ""
    trigger_type: str = "human"
    applied: bool = False
    reason: t.Optional[str] = None
    has_changes: bool = False
    added_count: int = 0
    removed_count: int = 0
    direct_count: int = 0
    indirect_count: int = 0
    metadata_count: int = 0
    backfill_count: int = 0
    duration_ms: int = 0
    error_message: t.Optional[str] = None
    created_at: str = ""
    # Full plan result (changes, backfills, test_results) — only in detail
    plan_result: t.Optional[PlanResult] = None


class ApplyHistoryListResponse(BaseModel):
    """Paginated apply history list."""
    records: t.List[ApplyHistoryRecord] = []
    total: int = 0


# --- Knowledge Document Responses ---

class KnowledgeDocumentResponse(BaseModel):
    """Single knowledge document metadata (content returned separately)."""
    id: str
    workspace_path: str
    title: str
    filename: str
    content_type: str = "markdown"
    file_size: int = 0
    tags: t.Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class KnowledgeListResponse(BaseModel):
    """Paginated knowledge document listing."""
    documents: t.List[KnowledgeDocumentResponse] = []
    total: int = 0


class KnowledgeContentResponse(BaseModel):
    """Document content read from disk."""
    id: str
    title: str
    content_type: str
    content: str = ""
    size: int = 0
