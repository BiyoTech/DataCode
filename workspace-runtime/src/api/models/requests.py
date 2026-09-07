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
    restate_models: Optional[List[str]] = None
    auto_apply: bool = False
    # Accepted only to reject it explicitly: an environment used to be created as
    # a side effect of ``apply`` + ``create_from``. Environments are now registry
    # objects (branch + worktree + virtual environment + deployment), so silently
    # ignoring the field would hide a request that no longer does what it says.
    create_from: Optional[str] = None

    @field_validator("create_from")
    @classmethod
    def _reject_create_from(cls, v: Optional[str]) -> Optional[str]:
        if v:
            raise ValueError(
                "plan_options.create_from is no longer supported: create the "
                "environment with POST /api/v1/workspaces/{workspace_id}/"
                "environments, then plan inside its working copy"
            )
        return v


class PlanPreviewRequest(WorkspacePathMixin):
    """Preview a plan for the environment this working copy is checked out to.

    ``environment`` is mandatory and must name a registered environment: the
    platform never infers a target nor creates one on the fly.
    """

    environment: str = Field(min_length=1, max_length=40)
    gateway: Optional[str] = None
    plan_dates: Optional[PlanDates] = None
    plan_options: Optional[PlanOptions] = None


class PlanApplyRequest(WorkspacePathMixin):
    environment: str = Field(min_length=1, max_length=40)
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


class RepairStateSchemaRequest(WorkspacePathMixin):
    """Reset the platform-managed ``state_schema`` in the working copy.

    Carries only the workspace path: the managed value is derived from the
    workspace id, never supplied by the client, so a tampered request cannot
    point the state at another workspace's schema.
    """


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

class IdentityMixin(BaseModel):
    """Caller identity injected by the gateway from the verified JWT.

    Both fields are always overwritten by the gateway, so a client-supplied
    value can never be used to impersonate another user or tenant.
    """

    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class CreateWorkspaceRequest(IdentityMixin):
    name: str = Field(min_length=1, max_length=128)
    dialect: str = "duckdb"
    git_repo_url: str = ""


class OpenWorkspaceRequest(IdentityMixin):
    """Materialize the caller's working copy of one environment.

    Empty ``environment`` falls back to the workspace's ``default_env``.
    """

    environment: str = ""


class BindGitRepoRequest(IdentityMixin):
    workspace_id: str
    git_repo_url: str = Field(min_length=1, max_length=512)


class SyncWorkspaceRequest(IdentityMixin):
    workspace_id: str
    message: str = "Auto-save"
    # Environment whose worktree is committed and pushed; empty = default_env.
    environment: str = ""


# --- Environments ---

class CreateEnvironmentRequest(BaseModel):
    """Create a platform environment (branch + virtual env + deployment).

    ``name`` is validated by the registry against the platform naming rule
    (lowercase snake_case); ``base_env`` defaults to the workspace's production
    environment. ``trigger_type`` distinguishes human from agent activity in the
    audit trail: agents may operate inside a sandbox but never create or promote,
    which the gateway enforces through permissions.
    """

    name: str = Field(min_length=2, max_length=40)
    env_type: str = "sandbox"          # shared | sandbox
    base_env: str = ""
    ttl_hours: int = Field(default=0, ge=0, le=720)
    trigger_type: str = "human"       # human | agent | schedule


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


class GitFetchRequest(WorkspacePathMixin):
    user_id: str = ""
    remote: str = "origin"
    # Delete local refs whose remote counterpart is gone.
    prune: bool = True


class GitResetRequest(WorkspacePathMixin):
    # Target commit. Restricted to a hex object name by the endpoint so it can
    # never be mistaken for a git option.
    commit_hash: str
    # soft  : move HEAD only (changes stay staged)
    # mixed : move HEAD + reset index (changes stay in the working tree)
    # hard  : move HEAD + reset index + working tree (DISCARDS changes)
    mode: str = "mixed"


class GitMergeRequest(WorkspacePathMixin):
    """Merge one environment branch into another and publish the result.

    The promotional half of what used to be a single atomic "promote": it moves
    CODE only. Landing the data (plan & apply) and refreshing the orchestration
    (code-location reload) are separate, explicit steps on the target
    environment. ``workspace_path`` identifies the caller's working copy, which
    supplies the shared object database and the remote — it is never modified.
    """

    user_id: str = ""
    remote: str = "origin"
    source_branch: str
    target_branch: str
    # Empty means the default "Merge branch 'x' into 'y'" subject.
    message: str = ""


class GitSyncRequest(WorkspacePathMixin):
    """Merge a base branch INTO the branch checked out in this working copy.

    The resolution path for a conflicting promotion: conflicts are left in the
    working tree, where the user fixes them with the editor and the Git panel.
    """

    user_id: str = ""
    remote: str = "origin"
    base_branch: str
    message: str = ""


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


class SaveDraftRequest(WorkspacePathMixin):
    """Editor draft autosave (durability contract <=5s layer, design 07 Q6)."""

    file_path: str
    content: str = ""


class DeleteDraftRequest(WorkspacePathMixin):
    """Drop one file's draft after it was saved or its recovery discarded."""

    file_path: str


class MkdirRequest(WorkspacePathMixin):
    directory: str


class RenameRequest(WorkspacePathMixin):
    old_path: str
    new_path: str


# Git credential payloads live in control-service: it owns the table and is the
# only writer. This service reads credentials through internal repository calls,
# never from a request body.


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
