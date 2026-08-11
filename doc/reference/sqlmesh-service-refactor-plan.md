# SQLMesh Service Refactoring & Optimization Plan

> **Objective**: Refactor sqlmesh-service based on the SQLMesh SDK API, eliminate legacy dual-track implementation, fix critical bugs, improve architecture, and align with the platform design's gRPC-first strategy.

---

## Table of Contents

1. [Current Issues Summary](#1-current-issues-summary)
2. [Refactoring Principles](#2-refactoring-principles)
3. [Architecture Redesign](#3-architecture-redesign)
4. [Phase 1: Critical Bug Fixes & Cleanup](#4-phase-1-critical-bug-fixes--cleanup)
5. [Phase 2: Core Layer Unification](#5-phase-2-core-layer-unification)
6. [Phase 3: API Layer Restructuring](#6-phase-3-api-layer-restructuring)
7. [Phase 4: gRPC Service Implementation](#7-phase-4-grpc-service-implementation)
8. [Phase 5: Context Lifecycle Optimization](#8-phase-5-context-lifecycle-optimization)
9. [Phase 6: Security Hardening](#9-phase-6-security-hardening)
10. [Phase 7: Observability & Resilience](#10-phase-7-observability--resilience)
11. [Migration Path](#11-migration-path)
12. [Target Directory Structure](#12-target-directory-structure)

---

## 1. Current Issues Summary

### Critical Bugs

| # | Issue | Impact | Location |
|---|-------|--------|----------|
| BUG-1 | `UserGitCredentialRepository` missing `get_by_user()` method | `AttributeError` at runtime | `workspace/manager.py:411` → `repository.py` |
| BUG-2 | `config.py` missing `lock_type`/`lock_file_path`/`lock_redis_url`/`lock_timeout_seconds` | `concurrency.py` unusable | `config.py` |
| BUG-3 | `context_manager.py:get_context_with_lock` deadlock | `get_context` internally re-acquires lock | `core/context_manager.py` |
| BUG-4 | Worker task dict `_tasks` unbounded growth | Memory leak on long-running process | `worker/pool.py` |
| BUG-5 | `execute_sync` 60s polling blocks event loop | High-concurrency perf degradation | `main.py:169-174` |

### Architecture Issues

| # | Issue | Description |
|---|-------|-------------|
| ARCH-1 | **Dual-track implementation** | `core/` (legacy in-process) and `worker/` (subprocess) co-exist with duplicated logic and inconsistent serialization |
| ARCH-2 | **Monolithic main.py** | 970+ lines, all endpoints + request models in one file |
| ARCH-3 | **Empty api/ directory** | `src/api/__init__.py` exists but no route modules |
| ARCH-4 | **gRPC declared but not implemented** | `grpcio` in dependencies, port 50052 exposed, zero implementation |
| ARCH-5 | **No Context caching in Worker** | Each worker task creates a fresh `Context(load=True)` (~1-2s overhead) |
| ARCH-6 | **Lazy imports everywhere** | `from src.workspace.manager import ...` inside every handler function |

### Security Issues

| # | Issue | Description |
|---|-------|-------------|
| SEC-1 | Git token stored in plaintext | DB column `token TEXT` with comment "Encrypted" but no encryption |
| SEC-2 | SQL LIMIT string concatenation | `f"{sql} LIMIT {limit}"` without type validation on `limit` |
| SEC-3 | No `workspace_path` validation | API accepts arbitrary path strings without existence/scope check |
| SEC-4 | `execute_sync` no cancel on timeout | 504 returned but subprocess task keeps running |

---

## 2. Refactoring Principles

1. **SDK-first**: All SQLMesh operations MUST go through the official Python SDK (`sqlmesh.core.context.Context`), no subprocess CLI calls
2. **Single implementation**: Eliminate the `core/` vs `worker/` dual-track — one canonical implementation path
3. **gRPC-first internal API**: Service-to-service calls use gRPC; REST endpoints are thin wrappers for backward compatibility and health/debug
4. **Context pool with lifecycle management**: Cache `Context` instances per workspace with TTL-based eviction and health checks
5. **Workspace-scoped isolation**: Each workspace gets its own Context instance, write operations serialized via distributed lock
6. **Fail fast, fail clearly**: Structured error responses with error codes, not generic `{"error": str(e)}`

---

## 3. Architecture Redesign

### Before (Current)

```
main.py (970 lines, all endpoints)
  ├── execute_sync() → WorkerPoolManager.submit_task() → ProcessPoolExecutor
  │     └── worker/executor.py → SQLMesh Context (created per task, no cache)
  └── legacy core/ services (partially used, inconsistent)
```

### After (Target)

```
main.py (thin entry, ~50 lines)
  ├── api/rest/          ← REST route modules (backward compat + health/debug)
  │     ├── tasks.py
  │     ├── sqlmesh.py
  │     ├── workspace.py
  │     ├── git.py
  │     └── files.py
  ├── api/grpc/          ← gRPC service (primary internal API)
  │     └── sqlmesh_grpc.py
  ├── core/
  │     ├── context_pool.py    ← NEW: Context lifecycle pool with TTL + health
  │     ├── plan_service.py    ← Rewritten: canonical Plan operations
  │     ├── apply_service.py   ← NEW: extracted from plan_service
  │     ├── lineage_service.py ← Rewritten: richer lineage data
  │     ├── model_service.py   ← Rewritten: full model metadata
  │     ├── schema_service.py  ← Rewritten: catalog-aware discovery
  │     ├── query_service.py   ← Rewritten: safe LIMIT handling
  │     └── format.py          ← NEW: unified serialization helpers
  └── workspace/               ← Unchanged (well-structured, minor fixes)
```

### Key Change: Remove Worker Process Pool

**Why**: The ProcessPoolExecutor was introduced to solve Context thread-safety. But with a proper **Context Pool** (one Context per workspace, single-threaded access via lock), we can run in-process and avoid:

- ~1-2s Context creation overhead per task
- Inter-process serialization overhead (pickle)
- Memory duplication across worker processes
- The `_tasks` dict memory leak

**New concurrency model**:
- One `asyncio.Lock` per workspace for write operations
- SQLMesh SDK calls wrapped in `asyncio.to_thread()` to avoid blocking the event loop
- Read operations can share the same Context concurrently (SQLMesh Context reads are thread-safe)

---

## 4. Phase 1: Critical Bug Fixes & Cleanup

### BUG-1: Add `get_by_user()` to `UserGitCredentialRepository`

```python
# workspace/repository.py — add method
class UserGitCredentialRepository(BaseRepository):
    def get_by_user(self, user_id: str) -> List[UserGitCredentialModel]:
        """Get all credentials for a user."""
        with self.session_scope() as session:
            rows = session.query(UserGitCredentialModel).filter(
                UserGitCredentialModel.user_id == user_id
            ).all()
            return rows
```

### BUG-2: Add missing config fields

```python
# config.py — add to Settings class
lock_type: Literal["file", "redis", "none"] = "none"
lock_file_path: str = "/tmp/sqlmesh-service-locks"
lock_redis_url: Optional[str] = None
lock_timeout_seconds: int = 300
task_ttl_seconds: int = 3600        # Task result retention
workspace_ttl_seconds: int = 86400   # Workspace metadata retention
```

### BUG-3: Fix `get_context_with_lock` deadlock

Remove the `get_context_with_lock` method entirely — it will be replaced by the new `ContextPool` in Phase 2.

### BUG-4: Add task TTL and cleanup

```python
# worker/pool.py — add periodic cleanup
def _cleanup_expired_tasks(self):
    """Remove tasks older than TTL."""
    now = datetime.utcnow()
    expired = [
        tid for tid, task in self._tasks.items()
        if (now - task.created_at).total_seconds() > self._task_ttl
    ]
    for tid in expired:
        del self._tasks[tid]
    if expired:
        logger.info("cleaned_up_expired_tasks", count=len(expired))
```

### BUG-5: Replace polling with `asyncio.Event`

```python
# Replace polling loop with event-based wait
async def execute_sync(self, task_type: str, params: dict, workspace_path: str) -> dict:
    task_id, event = self.pool.submit_task(task_type, params, workspace_path)
    try:
        await asyncio.wait_for(event.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        self.pool.cancel_task(task_id)
        raise HTTPException(504, "Task execution timeout")
    task = self.pool.get_task_status(task_id)
    return task.result
```

### Cleanup: Delete `core/` legacy implementations

After Phase 2 is complete, delete:
- `core/context_manager.py` (replaced by `context_pool.py`)
- `core/__init__.py` legacy marker

---

## 5. Phase 2: Core Layer Unification

### 5.1 Context Pool

The single most impactful change. Replace the Worker Pool + ProcessPoolExecutor with an in-process Context pool.

```python
# core/context_pool.py
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from sqlmesh.core.context import Context
from sqlmesh.core.config import Config

import structlog
logger = structlog.get_logger()


@dataclass
class ContextEntry:
    """Cached SQLMesh Context with metadata."""
    context: Context
    workspace_path: str
    gateway: Optional[str]
    last_accessed: float = field(default_factory=time.time)
    last_write_at: float = 0.0
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_reads: int = 0
    is_valid: bool = True


class ContextPool:
    """
    Lifecycle pool for SQLMesh Context instances.

    Strategy:
    - One Context per (workspace_path, gateway) key
    - Write operations acquire write_lock (exclusive)
    - Read operations acquire read_lock then increment active_reads
    - TTL-based eviction for idle contexts
    - Health check before returning cached context
    """

    def __init__(
        self,
        max_size: int = 20,
        idle_ttl_seconds: int = 1800,     # 30 min idle eviction
        max_age_seconds: int = 7200,      # 2 hr hard eviction
        health_check_on_get: bool = True,
    ):
        self._pool: Dict[str, ContextEntry] = {}
        self._pool_lock = asyncio.Lock()
        self._max_size = max_size
        self._idle_ttl = idle_ttl_seconds
        self._max_age = max_age_seconds
        self._health_check = health_check_on_get
        logger.info(
            "context_pool_initialized",
            max_size=max_size,
            idle_ttl=idle_ttl_seconds,
            max_age=max_age_seconds,
        )

    def _key(self, workspace_path: str, gateway: Optional[str] = None) -> str:
        return f"{workspace_path}:{gateway or 'default'}"

    async def get_context(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        config: Optional[Config] = None,
    ) -> ContextEntry:
        """Get or create a Context for the given workspace."""
        key = self._key(workspace_path, gateway)
        async with self._pool_lock:
            entry = self._pool.get(key)
            if entry and entry.is_valid:
                entry.last_accessed = time.time()
                # Refresh if files may have changed
                try:
                    await asyncio.to_thread(entry.context.refresh)
                except Exception as e:
                    logger.warning("context_refresh_failed", key=key, error=str(e))
                    entry.is_valid = False

            if not entry or not entry.is_valid:
                # Evict LRU if at capacity
                if len(self._pool) >= self._max_size:
                    await self._evict_lru()
                entry = await self._create_entry(workspace_path, gateway, config)
                self._pool[key] = entry

            return entry

    async def _create_entry(
        self,
        workspace_path: str,
        gateway: Optional[str],
        config: Optional[Config],
    ) -> ContextEntry:
        """Create a new Context (expensive, ~1-2s)."""
        logger.info("creating_context", workspace_path=workspace_path, gateway=gateway)
        kwargs = {"paths": workspace_path, "load": True}
        if gateway:
            kwargs["gateway"] = gateway
        if config:
            kwargs["config"] = config

        context = await asyncio.to_thread(Context, **kwargs)
        return ContextEntry(
            context=context,
            workspace_path=workspace_path,
            gateway=gateway,
        )

    async def invalidate(self, workspace_path: str, gateway: Optional[str] = None):
        """Invalidate cached context after write operations."""
        key = self._key(workspace_path, gateway)
        async with self._pool_lock:
            entry = self._pool.pop(key, None)
            if entry:
                logger.info("context_invalidated", key=key)
                try:
                    await asyncio.to_thread(entry.context.engine_adapter.close)
                except Exception:
                    pass

    async def _evict_lru(self):
        """Evict the least recently used idle entry."""
        now = time.time()
        candidates = [
            (k, e) for k, e in self._pool.items()
            if e.active_reads == 0 and (now - e.last_accessed) > self._idle_ttl
        ]
        if candidates:
            # Sort by last accessed, evict oldest
            candidates.sort(key=lambda x: x[1].last_accessed)
            key, entry = candidates[0]
            del self._pool[key]
            try:
                await asyncio.to_thread(entry.context.engine_adapter.close)
            except Exception:
                pass
            logger.info("context_evicted_lru", key=key)
        elif self._pool:
            # Force evict oldest even if still "active"
            oldest_key = min(self._pool, key=lambda k: self._pool[k].last_accessed)
            entry = self._pool.pop(oldest_key)
            logger.warning("context_force_evicted", key=oldest_key)

    async def start_periodic_cleanup(self, interval_seconds: int = 300):
        """Background task to evict expired entries."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self._evict_expired()

    async def _evict_expired(self):
        """Evict entries exceeding TTL."""
        now = time.time()
        expired_keys = []
        async with self._pool_lock:
            for key, entry in self._pool.items():
                age = now - entry.last_accessed
                if entry.active_reads == 0 and age > self._max_age:
                    expired_keys.append(key)
            for key in expired_keys:
                entry = self._pool.pop(key)
                try:
                    await asyncio.to_thread(entry.context.engine_adapter.close)
                except Exception:
                    pass
        if expired_keys:
            logger.info("evicted_expired_contexts", count=len(expired_keys))

    async def shutdown(self):
        """Close all contexts on shutdown."""
        async with self._pool_lock:
            for key, entry in self._pool.items():
                try:
                    await asyncio.to_thread(entry.context.engine_adapter.close)
                except Exception:
                    pass
            self._pool.clear()
            logger.info("context_pool_shutdown")


# Singleton instance
_pool: Optional[ContextPool] = None


def get_context_pool() -> ContextPool:
    global _pool
    if _pool is None:
        raise RuntimeError("ContextPool not initialized. Call init_context_pool() first.")
    return _pool


def init_context_pool(**kwargs) -> ContextPool:
    global _pool
    _pool = ContextPool(**kwargs)
    return _pool
```

### 5.2 Canonical Service Implementations

Each service uses the ContextPool and provides a single, well-tested implementation.

#### Plan Service (Rewritten)

```python
# core/plan_service.py
import asyncio
from typing import Any, Dict, Optional
from sqlmesh.core.plan import Plan

from src.core.context_pool import get_context_pool
from src.core.format import format_plan_result, format_context_diff
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PlanService:
    """Canonical Plan operations using SQLMesh SDK."""

    async def preview(
        self,
        workspace_path: str,
        environment: str = "dev",
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        select_models: Optional[list] = None,
        skip_tests: bool = False,
        forward_only: bool = False,
        no_prompts: bool = True,
    ) -> Dict[str, Any]:
        """Create a plan preview without applying."""
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        # Acquire write lock even for preview (plan loads state)
        async with entry.write_lock:
            try:
                plan = await asyncio.to_thread(
                    entry.context.plan,
                    environment=environment,
                    start=start,
                    end=end,
                    select_models=select_models,
                    skip_tests=skip_tests,
                    forward_only=forward_only,
                    no_prompts=no_prompts,
                    auto_apply=False,
                )
                logger.info(
                    "plan_preview_created",
                    workspace_path=workspace_path,
                    environment=environment,
                    has_changes=plan.has_changes,
                )
                return format_plan_result(plan)
            except Exception as e:
                logger.error("plan_preview_failed", error=str(e), workspace_path=workspace_path)
                raise

    async def apply(
        self,
        workspace_path: str,
        environment: str = "dev",
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        select_models: Optional[list] = None,
        skip_tests: bool = False,
        forward_only: bool = False,
        auto_apply: bool = True,
        no_prompts: bool = True,
    ) -> Dict[str, Any]:
        """Create and apply a plan."""
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        async with entry.write_lock:
            try:
                plan = await asyncio.to_thread(
                    entry.context.plan,
                    environment=environment,
                    start=start,
                    end=end,
                    select_models=select_models,
                    skip_tests=skip_tests,
                    forward_only=forward_only,
                    no_prompts=no_prompts,
                    auto_apply=False,
                )
                result = format_plan_result(plan)

                if not plan.has_changes:
                    logger.info("plan_apply_no_changes", workspace_path=workspace_path)
                    return {**result, "applied": False, "reason": "no_changes"}

                # Apply the plan
                await asyncio.to_thread(entry.context.apply, plan)
                logger.info(
                    "plan_applied",
                    workspace_path=workspace_path,
                    environment=environment,
                )

                # Invalidate context after write to ensure fresh state on next access
                await pool.invalidate(workspace_path, gateway)

                return {**result, "applied": True}
            except Exception as e:
                logger.error("plan_apply_failed", error=str(e), workspace_path=workspace_path)
                raise


# Singleton
_plan_service: Optional[PlanService] = None


def get_plan_service() -> PlanService:
    global _plan_service
    if _plan_service is None:
        _plan_service = PlanService()
    return _plan_service
```

#### Unified Serialization Helpers

```python
# core/format.py
"""Unified serialization helpers for SQLMesh SDK objects."""
from typing import Any, Dict, List, Optional
from sqlmesh.core.plan import Plan
from sqlmesh.core.snapshot import Snapshot
from sqlmesh.core.model import Model


def format_plan_result(plan: Plan) -> Dict[str, Any]:
    """Serialize a Plan object to API response dict."""
    context_diff = plan.context_diff
    return {
        "environment": plan.environment,
        "has_changes": plan.has_changes,
        "requires_backfill": plan.requires_backfill,
        "context_diff": format_context_diff(context_diff) if context_diff else None,
        "snapshots": {
            s.name: format_snapshot_summary(s)
            for s in plan.snapshots.values()
        } if plan.snapshots else {},
    }


def format_context_diff(context_diff) -> Dict[str, Any]:
    """Serialize ContextDiff with detailed change information."""
    result = {
        "modified": [],
        "added": [],
        "removed": [],
        "new_snapshots": [],
    }
    if hasattr(context_diff, "modified_snapshots") and context_diff.modified_snapshots:
        for sid, (old, new) in context_diff.modified_snapshots.items():
            result["modified"].append({
                "name": new.name,
                "change_category": str(new.change_category) if new.change_category else None,
                "is_breaking": new.change_category and "breaking" in str(new.change_category).lower(),
                "text_diff": old.text_diff(new) if hasattr(old, 'text_diff') else None,
            })
    if hasattr(context_diff, "added") and context_diff.added:
        result["added"] = list(context_diff.added)
    if hasattr(context_diff, "removed") and context_diff.removed:
        result["removed"] = list(context_diff.removed)
    return result


def format_snapshot_summary(snapshot: Snapshot) -> Dict[str, Any]:
    """Lightweight snapshot summary for plan results."""
    return {
        "name": snapshot.name,
        "version": snapshot.version,
        "change_category": str(snapshot.change_category) if snapshot.change_category else None,
        "intervals_count": len(snapshot.intervals) if snapshot.intervals else 0,
    }


def format_model_detail(model: Model) -> Dict[str, Any]:
    """Full model detail for get_model endpoint."""
    return {
        "fqn": model.fqn,
        "name": model.name,
        "kind": str(model.kind) if model.kind else None,
        "cron": model.cron,
        "dialect": model.dialect,
        "description": model.description,
        "owner": model.owner,
        "tags": list(model.tags) if model.tags else [],
        "start": str(model.start) if model.start else None,
        "end": str(model.end) if model.end else None,
        "columns": {
            col: str(dtype) for col, dtype in model.columns_to_types.items()
        } if model.columns_to_types else None,
        "depends_on": list(model.depends_on) if model.depends_on else [],
        "partitioned_by": [str(p) for p in model.partitioned_by] if model.partitioned_by else None,
        "file_path": str(model._path) if hasattr(model, '_path') and model._path else None,
        "fingerprint": str(model.fingerprint) if hasattr(model, 'fingerprint') else None,
    }


def format_model_list_item(model: Model) -> Dict[str, Any]:
    """Lightweight model info for list endpoint."""
    return {
        "fqn": model.fqn,
        "name": model.name,
        "kind": str(model.kind) if model.kind else None,
        "cron": model.cron,
        "description": model.description,
        "owner": model.owner,
        "tags": list(model.tags) if model.tags else [],
        "depends_on_count": len(model.depends_on) if model.depends_on else 0,
    }
```

### 5.3 Other Canonical Services (Pattern)

All other services (`lineage_service`, `model_service`, `schema_service`, `query_service`) follow the same pattern:

1. Get `ContextEntry` from `ContextPool`
2. Acquire `read_lock` (reads) or `write_lock` (writes)
3. Call `asyncio.to_thread(context.<method>, ...)` to offload blocking SQLMesh SDK calls
4. Serialize result via `format.py` helpers
5. Return structured dict

---

## 6. Phase 3: API Layer Restructuring

### Split main.py into Route Modules

```
api/
├── __init__.py
├── deps.py              # Shared dependencies (get_context_pool, get_plan_service, etc.)
├── rest/
│   ├── __init__.py
│   ├── tasks.py         # Task submission/status/cancel
│   ├── sqlmesh.py       # Plan/Apply/Models/Lineage/Schema/Query
│   ├── workspace.py     # Workspace CRUD + bind-git + sync
│   ├── git.py           # Git status/stage/commit/push/pull/branch/checkout
│   └── files.py         # File read/write/list/delete/mkdir/rename
├── grpc/
│   ├── __init__.py
│   └── sqlmesh_servicer.py
└── models/
    ├── __init__.py
    ├── requests.py       # Pydantic request models
    └── responses.py      # Pydantic response models
```

### Example: `api/rest/sqlmesh.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from src.api.models.requests import PlanPreviewRequest, PlanApplyRequest
from src.api.models.responses import PlanResponse, ModelListResponse
from src.api.deps import get_plan_service, get_model_service, get_lineage_service
from src.core.plan_service import PlanService

router = APIRouter(prefix="/api/v1", tags=["sqlmesh"])


@router.post("/plan", response_model=PlanResponse)
async def plan_preview(
    request: PlanPreviewRequest,
    plan_svc: PlanService = Depends(get_plan_service),
):
    """Create a plan preview without applying."""
    try:
        result = await plan_svc.preview(
            workspace_path=request.workspace_path,
            environment=request.environment,
            gateway=request.gateway,
            start=request.start,
            end=request.end,
            select_models=request.select_models,
            skip_tests=request.skip_tests,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply", response_model=PlanResponse)
async def plan_apply(
    request: PlanApplyRequest,
    plan_svc: PlanService = Depends(get_plan_service),
):
    """Create and apply a plan."""
    try:
        result = await plan_svc.apply(
            workspace_path=request.workspace_path,
            environment=request.environment,
            gateway=request.gateway,
            start=request.start,
            end=request.end,
            select_models=request.select_models,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ... models, lineage, schema, query endpoints
```

### Thin main.py

```python
# main.py — ~80 lines
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.core.context_pool import init_context_pool, get_context_pool
from src.config import get_settings
from src.api.rest import tasks, sqlmesh, workspace, git, files
from src.utils.logging import setup_logging

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = init_context_pool(
        max_size=settings.context_pool_max_size,
        idle_ttl_seconds=settings.context_idle_ttl,
    )
    # Start background cleanup
    import asyncio
    cleanup_task = asyncio.create_task(pool.start_periodic_cleanup())
    yield
    cleanup_task.cancel()
    await pool.shutdown()


app = FastAPI(title="SQLMesh Service", lifespan=lifespan)

# Register REST routes
app.include_router(tasks.router)
app.include_router(sqlmesh.router)
app.include_router(workspace.router)
app.include_router(git.router)
app.include_router(files.router)

# Health endpoint
@app.get("/health")
async def health():
    pool = get_context_pool()
    return {"status": "healthy", "context_pool_size": len(pool._pool)}
```

---

## 7. Phase 4: gRPC Service Implementation

### Proto Definition

Based on the plan's `proto/sqlmesh/v1/sqlmesh.proto`:

```protobuf
syntax = "proto3";
package sqlmesh.v1;

service SQLMeshService {
  rpc CreatePlan(CreatePlanRequest) returns (PlanResponse);
  rpc ApplyPlan(ApplyPlanRequest) returns (ApplyResponse);
  rpc GetLineage(GetLineageRequest) returns (LineageResponse);
  rpc DiscoverSchema(DiscoverSchemaRequest) returns (SchemaResponse);
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc GetModel(GetModelRequest) returns (ModelResponse);
  rpc ExecuteQuery(ExecuteQueryRequest) returns (QueryResponse);
  rpc ValidateModel(ValidateModelRequest) returns (ValidateResponse);
  rpc RenderModel(RenderModelRequest) returns (RenderResponse);
}

message CreatePlanRequest {
  string workspace_path = 1;
  string environment = 2;
  optional string gateway = 3;
  optional string start = 4;
  optional string end = 5;
  repeated string select_models = 6;
  bool skip_tests = 7;
  bool forward_only = 8;
}

message PlanResponse {
  string environment = 1;
  bool has_changes = 2;
  bool requires_backfill = 3;
  repeated ModelChange modified = 4;
  repeated string added = 5;
  repeated string removed = 6;
}

message ApplyResponse {
  bool applied = 1;
  string environment = 2;
  PlanResponse plan = 3;
  optional string error = 4;
}

message GetLineageRequest {
  string workspace_path = 1;
  optional string environment = 2;
  repeated string model_names = 3;  // empty = all models
}

message LineageResponse {
  repeated LineageNode nodes = 1;
  repeated LineageEdge edges = 2;
}

message LineageNode {
  string name = 1;
  string kind = 2;
  optional string description = 3;
  optional string owner = 4;
}

message LineageEdge {
  string source = 1;  // upstream model
  string target = 2;  // downstream model
}

message DiscoverSchemaRequest {
  string workspace_path = 1;
  optional string gateway = 2;
  optional string catalog = 3;
  optional string schema_name = 4;
  optional string table_name = 5;
}

message SchemaResponse {
  repeated TableSchema tables = 1;
}

message TableSchema {
  string catalog = 1;
  string schema = 2;
  string table = 3;
  repeated ColumnInfo columns = 4;
}

message ColumnInfo {
  string name = 1;
  string data_type = 2;
  bool nullable = 3;
  optional string comment = 4;
}

message ListModelsRequest {
  string workspace_path = 1;
  optional string environment = 2;
  optional string gateway = 3;
}

message ListModelsResponse {
  repeated ModelInfo models = 1;
}

message ModelInfo {
  string fqn = 1;
  string name = 2;
  string kind = 3;
  string cron = 4;
  optional string description = 5;
  optional string owner = 6;
  repeated string tags = 7;
  int32 depends_on_count = 8;
}

message GetModelRequest {
  string workspace_path = 1;
  string model_name = 2;
  optional string gateway = 3;
}

message ModelResponse {
  string fqn = 1;
  string name = 2;
  string kind = 3;
  string cron = 4;
  optional string dialect = 5;
  optional string description = 6;
  optional string owner = 7;
  repeated string tags = 8;
  map<string, string> columns = 9;
  repeated string depends_on = 10;
  optional string rendered_sql = 11;
}

message ExecuteQueryRequest {
  string workspace_path = 1;
  string sql = 2;
  optional string gateway = 3;
  int32 row_limit = 4;  // default 1000
}

message QueryResponse {
  repeated Row rows = 1;
  repeated string columns = 2;
  int64 execution_time_ms = 3;
  int32 row_count = 4;
}

message Row {
  map<string, string> values = 1;
}

message ValidateModelRequest {
  string workspace_path = 1;
  optional string gateway = 2;
}

message ValidateResponse {
  bool valid = 1;
  repeated string errors = 2;
  repeated string warnings = 3;
}

message RenderModelRequest {
  string workspace_path = 1;
  string model_name = 2;
  optional string gateway = 3;
  optional string start = 4;
  optional string end = 5;
}

message RenderResponse {
  string rendered_sql = 1;
  string dialect = 2;
}
```

### gRPC Servicer Implementation

```python
# api/grpc/sqlmesh_servicer.py
import grpc
from concurrent import futures
from typing import AsyncIterator
import asyncio

from src.core.context_pool import get_context_pool
from src.core.plan_service import get_plan_service
from src.core.model_service import get_model_service
from src.core.lineage_service import get_lineage_service
from src.core.schema_service import get_schema_service
from src.core.query_service import get_query_service
from src.utils.logging import get_logger

import sqlmesh_v1_pb2
import sqlmesh_v1_pb2_grpc

logger = get_logger(__name__)


class SQLMeshServicer(sqlmesh_v1_pb2_grpc.SQLMeshServiceServicer):
    """gRPC service implementation wrapping SQLMesh SDK operations."""

    async def CreatePlan(self, request, context):
        plan_svc = get_plan_service()
        try:
            result = await plan_svc.preview(
                workspace_path=request.workspace_path,
                environment=request.environment,
                gateway=request.gateway or None,
                start=request.start or None,
                end=request.end or None,
                select_models=list(request.select_models) or None,
                skip_tests=request.skip_tests,
                forward_only=request.forward_only,
            )
            return self._plan_response(result)
        except Exception as e:
            logger.error("grpc_create_plan_failed", error=str(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return sqlmesh_v1_pb2.PlanResponse()

    async def ApplyPlan(self, request, context):
        plan_svc = get_plan_service()
        try:
            result = await plan_svc.apply(
                workspace_path=request.workspace_path,
                environment=request.environment,
                gateway=request.gateway or None,
            )
            response = sqlmesh_v1_pb2.ApplyResponse(
                applied=result.get("applied", False),
                environment=result.get("environment", ""),
            )
            if "plan" in result:
                response.plan.CopyFrom(self._plan_response(result["plan"]))
            return response
        except Exception as e:
            logger.error("grpc_apply_plan_failed", error=str(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return sqlmesh_v1_pb2.ApplyResponse()

    # ... other RPC methods follow the same pattern

    def _plan_response(self, result: dict) -> sqlmesh_v1_pb2.PlanResponse:
        response = sqlmesh_v1_pb2.PlanResponse(
            environment=result.get("environment", ""),
            has_changes=result.get("has_changes", False),
            requires_backfill=result.get("requires_backfill", False),
        )
        # ... populate modified/added/removed from result
        return response
```

---

## 8. Phase 5: Context Lifecycle Optimization

### Problem

Current Worker Pool creates a new `Context(load=True)` for each task (~1-2s overhead). Context Pool caches and reuses instances.

### Optimization Strategy

| Aspect | Current | After Refactor |
|--------|---------|---------------|
| Context creation | Per task (no cache) | Cached per workspace, TTL eviction |
| Freshness | Always fresh | `context.refresh()` on get (fast, ~100ms) |
| Thread safety | Process isolation | `asyncio.Lock` per workspace |
| Memory | N×Worker processes | Single process, shared Contexts |
| Apply invalidation | N/A | `invalidate()` + next access creates fresh |

### Context Health Check

```python
# In ContextPool.get_context() — after refresh
async def _health_check(self, entry: ContextEntry) -> bool:
    """Verify context is usable by listing models."""
    try:
        models = await asyncio.to_thread(lambda: len(entry.context.models))
        return models >= 0  # Any non-exception result is healthy
    except Exception:
        return False
```

---

## 9. Phase 6: Security Hardening

### SEC-1: Encrypt Git Tokens

```python
# workspace/crypto.py
import hashlib
import base64
from cryptography.fernet import Fernet
from src.config import get_settings


def get_encryption_key() -> bytes:
    """Derive Fernet key from configured secret."""
    secret = get_settings().token_encryption_secret
    if not secret:
        raise ValueError("token_encryption_secret not configured")
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def encrypt_token(plaintext: str) -> str:
    key = get_encryption_key()
    return Fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    key = get_encryption_key()
    return Fernet(key).decrypt(ciphertext.encode()).decode()
```

Add to `config.py`:
```python
token_encryption_secret: str = ""  # MUST be set in production
```

### SEC-2: Safe LIMIT Handling

```python
# core/query_service.py
def _apply_limit(sql: str, limit: int) -> str:
    """Safely inject LIMIT clause with validated integer."""
    if not isinstance(limit, int) or limit < 0:
        raise ValueError(f"Invalid limit: {limit}. Must be a non-negative integer.")
    max_limit = get_settings().query_max_row_limit
    limit = min(limit, max_limit)
    # Use sqlglot parser instead of string concatenation
    from sqlglot import parse_one
    expr = parse_one(sql)
    expr.set("limit", limit)
    return expr.sql()
```

### SEC-3: Workspace Path Validation

```python
# api/deps.py
from pathlib import Path
from src.config import get_settings


def validate_workspace_path(workspace_path: str) -> Path:
    """Ensure workspace path exists and is within allowed base."""
    settings = get_settings()
    base = Path(settings.workspace_base_path).resolve()
    target = Path(workspace_path).resolve()

    if not str(target).startswith(str(base)):
        raise ValueError(f"Workspace path '{workspace_path}' is outside allowed base '{base}'")
    if not target.exists():
        raise ValueError(f"Workspace path does not exist: {workspace_path}")
    return target
```

Add to `config.py`:
```python
workspace_base_path: str = "/data/workspaces"
```

### SEC-4: Cancel on Timeout

Use `asyncio.wait_for` with `on_cleanup` callback to cancel orphaned tasks:

```python
async def execute_with_timeout(coro, timeout: int, task_id: str):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("task_timeout_cancelling", task_id=task_id)
        # Signal cancellation (SQLMesh doesn't support cancel, but we can
        # invalidate the context to force fresh state on next access)
        raise HTTPException(504, f"Task {task_id} timed out after {timeout}s")
```

---

## 10. Phase 7: Observability & Resilience

### Structured Error Responses

```python
# api/models/responses.py
from pydantic import BaseModel
from enum import Enum


class ErrorCode(str, Enum):
    CONTEXT_LOAD_FAILED = "CONTEXT_LOAD_FAILED"
    PLAN_FAILED = "PLAN_FAILED"
    APPLY_FAILED = "APPLY_FAILED"
    QUERY_FAILED = "QUERY_FAILED"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    INVALID_REQUEST = "INVALID_REQUEST"
    TIMEOUT = "TIMEOUT"
    CONCURRENT_WRITE = "CONCURRENT_WRITE"


class ErrorResponse(BaseModel):
    error_code: ErrorCode
    message: str
    detail: str | None = None
```

### Metrics Endpoint Enhancement

```python
@app.get("/api/v1/pool/metrics")
async def pool_metrics():
    pool = get_context_pool()
    return {
        "context_pool": {
            "size": len(pool._pool),
            "max_size": pool._max_size,
            "entries": [
                {
                    "key": k,
                    "workspace": e.workspace_path,
                    "last_accessed_ago_seconds": int(time.time() - e.last_accessed),
                    "active_reads": e.active_reads,
                }
                for k, e in pool._pool.items()
            ],
        },
    }
```

---

## 11. Migration Path

| Step | Action | Risk | Rollback |
|------|--------|------|----------|
| 1 | Fix BUG-1 to BUG-5 (no architecture change) | Low | Revert commit |
| 2 | Add `core/format.py` serialization helpers | Low | Not used yet |
| 3 | Add `core/context_pool.py` alongside existing `worker/` | Low | Feature flag: `use_context_pool=false` |
| 4 | Add `api/rest/` route modules, register in main.py alongside existing endpoints | Low | Feature flag per route |
| 5 | Add `api/models/` request/response Pydantic models | Low | Not used yet |
| 6 | Switch REST endpoints to use ContextPool + canonical services | Medium | Feature flag |
| 7 | Remove `worker/` directory and `core/context_manager.py` | Medium | Git revert |
| 8 | Add gRPC service on port 50052 | Low | New port, no REST impact |
| 9 | Security hardening (SEC-1 to SEC-4) | Low | Config toggle |
| 10 | Observability enhancements | Low | Additive |

**Recommended**: Add a `use_context_pool` feature flag in config so both implementations can co-exist during migration.

---

## 12. Target Directory Structure

```
sqlmesh-service/
├── Dockerfile
├── pyproject.toml
└── src/
    ├── __init__.py
    ├── main.py                     # Thin entry point (~80 lines)
    ├── config.py                   # Extended with new fields
    ├── api/
    │   ├── __init__.py
    │   ├── deps.py                 # Shared FastAPI dependencies
    │   ├── rest/
    │   │   ├── __init__.py
    │   │   ├── tasks.py            # Task submission/status/cancel
    │   │   ├── sqlmesh.py          # Plan/Apply/Models/Lineage/Schema/Query
    │   │   ├── workspace.py        # Workspace CRUD
    │   │   ├── git.py              # Git operations
    │   │   └── files.py            # File operations
    │   ├── grpc/
    │   │   ├── __init__.py
    │   │   └── sqlmesh_servicer.py # gRPC service impl
    │   └── models/
    │       ├── __init__.py
    │       ├── requests.py         # Pydantic request models
    │       └── responses.py        # Pydantic response models
    ├── core/
    │   ├── __init__.py
    │   ├── context_pool.py         # NEW: Context lifecycle pool
    │   ├── plan_service.py         # Rewritten: canonical
    │   ├── apply_service.py        # NEW: extracted
    │   ├── lineage_service.py      # Rewritten: richer data
    │   ├── model_service.py        # Rewritten: full metadata
    │   ├── schema_service.py       # Rewritten: catalog-aware
    │   ├── query_service.py        # Rewritten: safe LIMIT
    │   └── format.py               # NEW: unified serialization
    ├── utils/
    │   ├── __init__.py
    │   ├── concurrency.py          # Fixed: add missing config support
    │   ├── logging.py              # Unchanged
    │   └── errors.py               # NEW: structured error codes
    └── workspace/                  # Unchanged (minor fixes only)
        ├── __init__.py
        ├── crypto.py               # NEW: token encryption
        ├── database.py
        ├── file_ops.py
        ├── git_ops.py
        ├── manager.py
        ├── models.py
        └── repository.py           # Fixed: add get_by_user()
```

---

## 13. Multi-User / Multi-Tenant / Git Consistency

> This section addresses isolation and consistency concerns when extending Context Pool beyond the MVP single-user scenario.

### 13.1 Context Pool Key Strategy

**MVP (single user)**: Key = `workspace_path:gateway` — sufficient.

**Multi-tenant (future)**: Workspace paths are already tenant-scoped (e.g., `/data/workspaces/{tenant_id}/{project_id}/`), so path uniqueness guarantees tenant isolation. If paths could collide across tenants, extend the key:

```python
def _key(self, workspace_path: str, gateway: Optional[str] = None, tenant_id: str = "default") -> str:
    return f"{tenant_id}:{workspace_path}:{gateway or 'default'}"
```

### 13.2 Tenant Access Control

Context Pool remains **permission-agnostic** (correct separation of concerns). Access control is enforced at the API entry layer:

```python
# api/deps.py
async def validate_workspace_access(
    workspace_path: str,
    tenant_id: str,   # injected by auth middleware
    user_id: str,     # injected by auth middleware
) -> str:
    """Verify user belongs to the tenant that owns this workspace."""
    ws_repo = get_workspace_repository()
    ws = ws_repo.get_by_path(workspace_path)
    if not ws or ws.tenant_id != tenant_id:
        raise PermissionDeniedError(f"No access to workspace: {workspace_path}")
    return workspace_path
```

### 13.3 Plan/Apply Optimistic Locking (Multi-User Collaboration)

When multiple users can modify the same workspace, a Plan created at time T1 may be stale if another user writes files at T2 before Apply at T3.

**Solution**: State fingerprint (git commit hash) as optimistic lock:

```python
class PlanService:
    async def preview(self, workspace_path: str, ...) -> dict:
        result = format_plan_result(plan)
        # Capture workspace state at plan creation time
        result["state_fingerprint"] = await self._get_workspace_fingerprint(workspace_path)
        return result

    async def apply(self, workspace_path: str, *, expected_fingerprint: str = None, ...) -> dict:
        # Verify workspace hasn't been modified since plan was created
        if expected_fingerprint:
            current = await self._get_workspace_fingerprint(workspace_path)
            if current != expected_fingerprint:
                raise ConflictError(
                    "Workspace modified since plan creation. "
                    "Please re-run plan to get fresh changes."
                )
        # Proceed with apply...

    async def _get_workspace_fingerprint(self, workspace_path: str) -> str:
        """Use git HEAD commit hash as workspace state fingerprint."""
        import subprocess
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", workspace_path, "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
```

### 13.4 Git Operations → Context Invalidation

**Critical Rule**: Any Git operation that changes the working tree files MUST invalidate the cached Context.

| Git Operation | Changes Files? | Must Invalidate? |
|---------------|---------------|-----------------|
| `checkout <branch>` | ✅ | ✅ |
| `pull` | ✅ | ✅ |
| `merge` | ✅ | ✅ |
| `reset --hard` | ✅ | ✅ |
| `stash pop/apply` | ✅ | ✅ |
| `rebase` | ✅ | ✅ |
| `commit` | ❌ (index only) | ❌ |
| `push` | ❌ | ❌ |
| `status` | ❌ | ❌ |
| `log` | ❌ | ❌ |
| `stage/unstage` | ❌ | ❌ |
| `branch` (create/delete) | ❌ | ❌ |

**Implementation**: Wrap Git write operations with invalidation callback:

```python
# api/rest/git.py
@router.post("/api/v1/git/checkout")
async def git_checkout(request: CheckoutRequest):
    result = await git_ops.checkout(request.branch, request.workspace_path)
    # CRITICAL: Invalidate context after branch switch
    pool = get_context_pool()
    await pool.invalidate(request.workspace_path)
    return result


@router.post("/api/v1/git/pull")
async def git_pull(request: PullRequest):
    result = await git_ops.pull(request.workspace_path)
    await get_context_pool().invalidate(request.workspace_path)
    return result
```

### 13.5 File Write → Dirty Flag + Lazy Refresh

Writing model files through the file API makes the cached Context stale, but calling `refresh()` on every file write is wasteful (user might write 5 files then plan once).

**Solution**: Mark dirty, refresh lazily before SQLMesh operations:

```python
@dataclass
class ContextEntry:
    # ... existing fields ...
    dirty: bool = False  # Files changed since last refresh


# File write marks dirty
@router.post("/api/v1/files/write")
async def write_file(request: WriteFileRequest):
    await file_ops.write(request.workspace_path, request.path, request.content)
    pool = get_context_pool()
    key = pool._key(request.workspace_path)
    entry = pool._pool.get(key)
    if entry:
        entry.dirty = True
    return {"success": True}


# SQLMesh operations check dirty flag before executing
class PlanService:
    async def preview(self, workspace_path: str, ...) -> dict:
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        # Refresh if workspace files changed
        if entry.dirty:
            await asyncio.to_thread(entry.context.refresh)
            entry.dirty = False
        # ... proceed with plan
```

### 13.6 Summary: What Must Be Done Per Phase

| Phase | Required For | Action |
|-------|-------------|--------|
| MVP (single user) | ✅ Correctness | Git write ops → `invalidate()` + File write → dirty flag |
| Multi-user | Collaboration | Add state fingerprint optimistic lock on Apply |
| Multi-tenant | Isolation | API-layer access control + tenant-scoped workspace paths |

**Key Insight**: Context Pool's isolation model is **workspace-scoped** (one Context per workspace directory). This naturally maps to:
- Single user: one user ↔ one workspace ✅
- Multi-user teams: many users ↔ one workspace (write lock + optimistic lock needed)
- Multi-tenant: tenant boundary = workspace directory boundary (path isolation)
