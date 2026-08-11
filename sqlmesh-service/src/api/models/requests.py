"""Pydantic request models for REST API endpoints.

Matches SQLMesh web server's input models for compatibility,
adapted for multi-tenant workspace-based architecture.
"""

from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator


def _validated_workspace_path(value: str) -> str:
    from src.api.deps import validate_workspace_path

    return validate_workspace_path(value)


class WorkspacePathMixin(BaseModel):
    """Mixin that validates workspace_path on request models."""

    workspace_path: str

    @field_validator("workspace_path")
    @classmethod
    def _check_workspace_path(cls, v: str) -> str:
        return _validated_workspace_path(v)


# --- SQLMesh Plan Operations ---

class PlanDates(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None


class PlanOptions(BaseModel):
    skip_tests: bool = False
    skip_backfill: bool = False
    no_gaps: bool = False
    forward_only: bool = False
    no_auto_categorization: bool = False
    include_unmodified: bool = False
    create_from: Optional[str] = None
    restate_models: Optional[List[str]] = None
    auto_apply: bool = False


class PlanPreviewRequest(WorkspacePathMixin):
    environment: str = "dev"
    gateway: Optional[str] = None
    plan_dates: Optional[PlanDates] = None
    plan_options: Optional[PlanOptions] = None


class PlanApplyRequest(WorkspacePathMixin):
    environment: str = "dev"
    gateway: Optional[str] = None
    plan_dates: Optional[PlanDates] = None
    plan_options: Optional[PlanOptions] = None
    categories: Optional[dict] = None  # model_name -> SnapshotChangeCategory
    expected_fingerprint: Optional[str] = None  # Optimistic lock
    # Attribution fields injected by CLI wrapper (agent) or frontend (human)
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    trigger_type: Optional[str] = None  # "human" | "agent"


class PlanCancelRequest(WorkspacePathMixin):
    gateway: Optional[str] = None


# --- SQLMesh Model Operations ---

class RenderModelRequest(WorkspacePathMixin):
    model_name: str
    gateway: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    execution_time: Optional[str] = None
    expand: Union[bool, List[str]] = False
    pretty: bool = True
    dialect: Optional[str] = None


class ValidateRequest(WorkspacePathMixin):
    gateway: Optional[str] = None


# --- SQLMesh Query / Evaluate ---

class QueryRequest(WorkspacePathMixin):
    sql: str
    gateway: Optional[str] = None
    row_limit: int = Field(default=0, ge=0, le=10000)


class EvaluateRequest(WorkspacePathMixin):
    model: str
    gateway: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    execution_time: Optional[str] = None
    limit: int = 1000


# --- SQLMesh Test ---

class TestRequest(WorkspacePathMixin):
    gateway: Optional[str] = None
    test: Optional[str] = None  # Specific test path, None = all tests
    verbosity: str = "default"  # default, verbose, quiet


# --- SQLMesh Table Diff ---

class TableDiffRequest(WorkspacePathMixin):
    source: str
    target: str
    gateway: Optional[str] = None
    on: Optional[str] = None
    model_or_snapshot: Optional[str] = None
    where: Optional[str] = None
    temp_schema: Optional[str] = None
    limit: int = 20


# --- SQLMesh Schema Discovery ---

class SchemaDiscoverRequest(WorkspacePathMixin):
    gateway: Optional[str] = None
    schema_name: str = ""
    table_name: str = ""


# --- External Models Import ---

class ListSchemasRequest(WorkspacePathMixin):
    gateway: Optional[str] = None


class ImportExternalModelsRequest(WorkspacePathMixin):
    gateway: Optional[str] = None
    schema: str
    tables: List[str] = []       # empty = import all tables
    include: str = ""            # comma-separated glob patterns
    exclude: str = ""            # comma-separated glob patterns
    samples: int = 3
    strict: bool = False


# --- Workspace ---

class CreateWorkspaceRequest(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    name: str
    dialect: str = "duckdb"
    git_repo_url: str = ""


class BindGitRepoRequest(BaseModel):
    workspace_id: str
    user_id: str = ""
    git_repo_url: str


class SyncWorkspaceRequest(BaseModel):
    workspace_id: str
    message: str = "Auto-save"


# --- Git ---

class GitStageRequest(WorkspacePathMixin):
    paths: List[str] = []


class GitUnstageRequest(WorkspacePathMixin):
    paths: List[str] = []


class GitCommitRequest(WorkspacePathMixin):
    message: str


class GitPushRequest(WorkspacePathMixin):
    user_id: str = ""
    remote: str = "origin"
    branch: str = ""


class GitPullRequest(WorkspacePathMixin):
    user_id: str = ""
    remote: str = "origin"
    branch: str = ""


class GitCheckoutRequest(WorkspacePathMixin):
    branch: str
    create: bool = False


class GitPublishRequest(WorkspacePathMixin):
    user_id: str = ""
    provider: str = "gitea"
    repo_name: str
    description: str = ""
    is_private: bool = True


class GitInitRequest(WorkspacePathMixin):
    pass


# --- Files ---

class WriteFileRequest(WorkspacePathMixin):
    file_path: str
    content: str
    new_path: Optional[str] = None  # For rename


class DeleteFileRequest(WorkspacePathMixin):
    file_path: str


class MkdirRequest(WorkspacePathMixin):
    directory: str


class RenameRequest(WorkspacePathMixin):
    old_path: str
    new_path: str


# --- Git Credentials ---

class CreateGitCredentialRequest(BaseModel):
    user_id: str = ""
    provider: str = "gitea"
    nickname: str = ""
    auth_type: str = "pat"
    token: str
    email: str = ""
    username: str = ""
    server_url: str = ""


class UpdateGitCredentialRequest(BaseModel):
    nickname: str = ""
    token: str = ""
    email: str = ""
    username: str = ""
    server_url: str = ""


# --- Knowledge Documents ---

class CreateKnowledgeRequest(WorkspacePathMixin):
    """Create a text/Markdown knowledge document."""
    title: str
    content: str = ""  # Markdown / plain text body
    tags: Optional[str] = None  # comma-separated


class DeleteKnowledgeRequest(WorkspacePathMixin):
    """Delete a knowledge document (metadata + file on disk)."""
    document_id: str


class ListKnowledgeRequest(WorkspacePathMixin):
    """List knowledge documents for a workspace, with optional search."""
    search: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)
