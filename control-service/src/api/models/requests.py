"""Request models for control-service endpoints.

Only the control-plane surface is modelled here: workspace metadata, the
environment registry and Git credentials. The working-copy surface (plan,
apply, files, ...) belongs to workspace-runtime.
"""

from typing import Optional

from pydantic import BaseModel, Field


class IdentityMixin(BaseModel):
    """Caller identity injected by the gateway from the verified JWT.

    Both fields are always overwritten by the gateway, so a client-supplied
    value can never be used to impersonate another user or tenant.
    """

    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


# --- Workspaces ---


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


# --- Git credentials ---


class CreateGitCredentialRequest(BaseModel):
    """Register a Git provider credential (PAT or OAuth link) for the caller."""

    provider: str = Field(min_length=1, max_length=32)
    nickname: str = Field(default="", max_length=128)
    auth_type: str = Field(default="pat", pattern="^(pat|oauth)$")
    token: str = Field(min_length=1)
    email: str = Field(default="", max_length=256)
    username: str = Field(default="", max_length=128)
    server_url: str = Field(default="", max_length=512)
    is_default: bool = False


class UpdateGitCredentialRequest(BaseModel):
    """Update the mutable fields of a stored credential."""

    nickname: Optional[str] = Field(default=None, max_length=128)
    token: Optional[str] = None
    email: Optional[str] = Field(default=None, max_length=256)
    username: Optional[str] = Field(default=None, max_length=128)
    server_url: Optional[str] = Field(default=None, max_length=512)
    is_default: Optional[bool] = None
