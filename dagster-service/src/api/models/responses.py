"""Response body models for the dagster-service REST API.

All timestamps are epoch seconds (UTC). The frontend converts to the user's
local timezone for display.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ModeInfo(BaseModel):
    """Active Dagster mode + endpoint info."""

    mode: str
    graphql_url: str
    code_location_host: str
    code_location_port: int


class ProcessStatus(BaseModel):
    name: str
    pid: Optional[int] = None
    alive: bool
    uptime_seconds: Optional[float] = None
    # Supervision state: how often the supervisor respawned this process inside
    # the current window, and whether it gave up on it.
    restart_count: int = 0
    abandoned: bool = False


class HealthResponse(BaseModel):
    status: str
    service: str
    dagster_mode: str
    processes: list[ProcessStatus] = []


class AssetInfo(BaseModel):
    """A single asset node for DAG rendering."""

    asset_key: list[str]
    asset_key_str: str
    # Human-friendly display name. For SQLMesh-bridged assets this is the model
    # display name (``schema.table``) matching /api/v1/sqlmesh/models; for native
    # Dagster assets it falls back to the asset key string.
    name: Optional[str] = None
    group_name: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    deps: list[str] = []


class AssetDagGraph(BaseModel):
    """Full asset dependency graph (nodes + edges)."""

    nodes: list[AssetInfo] = []
    edges: list[dict[str, str]] = []  # {"source": key_str, "target": key_str}


class RunInfo(BaseModel):
    run_id: str
    status: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    job_name: Optional[str] = None


class RunLogEvent(BaseModel):
    message: Optional[str] = None
    timestamp: Optional[str] = None
    level: Optional[str] = None
    step_key: Optional[str] = None


class ScheduleInfo(BaseModel):
    name: str
    cron_schedule: Optional[str] = None
    pipeline_name: Optional[str] = None
    status: Optional[str] = None


class DeploymentInfo(BaseModel):
    """One environment's Dagster deployment and the state of its code location.

    ``location_name`` equals ``deployment_name``: the deployment IS the code
    location, exposed under both names so callers can speak either vocabulary.
    """

    deployment_name: str
    location_name: str
    workspace_path: str
    tenant_id: str
    workspace_id: str
    environment: str
    env_type: str
    schedules_enabled: bool
    host: str
    port: int
    definitions: Optional[str] = None
    alive: bool = False


class DeploymentAssetDiff(BaseModel):
    """Asset-key level difference between two deployments."""

    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []


class DeploymentScheduleChange(BaseModel):
    """One schedule that differs between two deployments."""

    name: str
    change: str  # added | removed | modified
    cron: str = ""
    base_cron: str = ""
    job: str = ""


class DeploymentDiff(BaseModel):
    """Orchestration-graph difference between a deployment and its base."""

    deployment: str
    base: str
    assets: DeploymentAssetDiff
    schedule_changes: list[DeploymentScheduleChange] = []


class OperationResult(BaseModel):
    """Generic success envelope for mutations."""

    success: bool = True
    detail: Optional[dict[str, Any]] = None
