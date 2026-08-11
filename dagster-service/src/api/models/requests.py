"""Request body models for the dagster-service REST API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TriggerRunRequest(BaseModel):
    """Launch a job run in a workspace's code location."""

    workspace_path: str = Field(..., description="Workspace root path")
    job_name: str = Field(..., description="Dagster job name to launch")
    repository_name: str = Field("__repository__", description="Repository name in the location")
    run_config: Optional[dict[str, Any]] = Field(
        default=None, description="Optional Dagster run config"
    )


class ReloadCodeLocationRequest(BaseModel):
    """Reload a workspace's code location after user code changes."""

    workspace_path: str = Field(..., description="Workspace root path")


class EnsureCodeLocationRequest(BaseModel):
    """Ensure a workspace's code location gRPC server is running."""

    workspace_path: str = Field(..., description="Workspace root path")
