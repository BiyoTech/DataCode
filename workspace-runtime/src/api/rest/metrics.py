"""Prometheus exposition endpoint (custom HPA metrics).

## Why a hand-written exposition

The service needs exactly a handful of gauges, all derived from state that is
already computed (the executor manager's live children and the metadata
cache's entry count). Pulling in prometheus_client for that adds a dependency
and a background collection model to what is a pure read of two in-memory
dicts — the text exposition format for gauges is three lines of code and
stays fully under our control.

## The HPA contract

``active_contexts_per_pod`` is the metric name the production HPA scales on
(architecture doc §4.1):

    metrics:
      - type: Pods
        pods: { metric: { name: active_contexts_per_pod } }
        target: { type: AverageValue, averageValue: "12" }

Since the P6 close-out the pod holds NO in-process Contexts; the value this
exposes is the number of live executor sandboxes (one per working copy +
gateway), which is what actually pins workspace memory and an engine
connection now. The series name is kept: the HPA contract does not care where
the Context lives, only what one unit costs.

Prometheus Adapter (or KEDA) scrapes this endpoint, matches the series, and
serves it to the HPA's custom-metrics API under that name.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["metrics"])


def _sandbox_counts() -> dict:
    """Executor / metadata-cache facts for the gauges (flat for exposition)."""
    from src.executor.process import get_executor_manager
    from src.sqlmesh.metadata_context import get_metadata_context

    active = len(get_executor_manager().active_workspaces())
    return {
        "active_contexts_per_pod": active,
        "datacode_metadata_cache_entries": get_metadata_context().cached_entry_count(),
    }


# HELP/TYPE per metric; the emit order follows the dict so the exposition is
# byte-stable across scrapes (identical content → no spurious series churn).
_HELP = {
    "active_contexts_per_pod": (
        "Live executor sandboxes held by this pod (one per working copy and "
        "gateway). The HPA scaling signal: each sandbox pins workspace memory "
        "and an engine connection."
    ),
    "datacode_metadata_cache_entries": (
        "Cached read-side metadata payloads (model lists, lineage, environments)."
    ),
}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """Expose executor / metadata-cache gauges in the Prometheus text format."""
    lines: list[str] = []
    for name, value in _sandbox_counts().items():
        help_text = _HELP.get(name, "")
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
