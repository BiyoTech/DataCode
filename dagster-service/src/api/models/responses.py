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


class CodeLocationInfo(BaseModel):
    location_name: str
    host: str
    port: int
    definitions: Optional[str] = None


class OperationResult(BaseModel):
    """Generic success envelope for mutations."""

    success: bool = True
    detail: Optional[dict[str, Any]] = None
