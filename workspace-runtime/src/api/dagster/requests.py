"""Request body models for the dagster-service REST API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TriggerRunRequest(BaseModel):
    """Launch a job run in a deployment's code location."""

    workspace_path: str = Field(..., description="Environment working-copy path")
    job_name: str = Field(..., description="Dagster job name to launch")
    repository_name: str = Field("__repository__", description="Repository name in the location")
    run_config: Optional[dict[str, Any]] = Field(
        default=None, description="Optional Dagster run config"
    )


class CodeLocationHeartbeatRequest(BaseModel):
    """The control plane reporting which code location revisions it now serves.

    ``acknowledged`` maps ``location_name -> revision`` and must carry the
    revision the control plane actually LOADED, not the one it last read: the
    acknowledgement is what tells this replica its reload became visible.
    """

    control_plane_id: str = Field(
        default="",
        description="Identifier of the control-plane syncer, for log correlation",
    )
    acknowledged: dict[str, int] = Field(
        default_factory=dict,
        description="Loaded code locations as {location_name: revision}",
    )


class DeploymentRequest(BaseModel):
    """Ensure or reload the deployment backing one environment working copy.

    ``workspace_path`` alone is sufficient: it encodes the tenant, workspace,
    user and environment, and the deployment name is a pure function of the last
    two. The remaining fields carry the environment registry's POLICY, which no
    path can express; when omitted the safe default applies (only the production
    environment is a production deployment, and only production is scheduled).
    """

    workspace_path: str = Field(..., description="Environment working-copy path")
    environment: str = Field(
        default="",
        description=(
            "Expected environment name. Confirms the working copy rather than "
            "redirecting it: a mismatch is rejected."
        ),
    )
    env_type: str = Field(
        default="",
        description="Deployment type from the registry: production | shared | sandbox",
    )
    schedules_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Whether the daemon may run this deployment's schedules. Defaults to "
            "true only for the production deployment."
        ),
    )


class ReloadRequest(DeploymentRequest):
    """Start (or join) a reload job for one environment working copy.

    ``source`` is attribution, not behaviour: an install triggered from the UI and
    one triggered from the CLI do exactly the same thing, and the manifest records
    which it was so a surprising dependency tree can be traced to its trigger.
    """

    source: str = Field(
        default="ui",
        description="Where the reload came from: ui | cli",
    )
