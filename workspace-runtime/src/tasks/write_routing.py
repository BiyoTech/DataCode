"""SQLMesh write routing — the L3→L2 split for `apply` (design 04 §3.5, Q5).

One decision lives here: an ``apply`` needs a WRITABLE warehouse account, and a
project containing Python models needs to execute user code. Q5 forbids granting
both to one sandbox, because user code holding a writable account can bypass
SQLMesh entirely and issue arbitrary DDL.

So the work is routed, not compromised:

* **SQL-only project** — a single L2 task. Nothing executes but SQL, so the
  writable account is safe where it is.
* **Project with Python** — two tasks. Phase 1 renders the models to SQL in
  L3-base with a READ-ONLY account; phase 2 applies the rendered project in L2
  with the writable one.

The property that makes the split sound rather than cosmetic: phase 2's SQL-only
check stays ON. The rendered project genuinely contains no Python, so the check
passes on its own merits. A version of this that told L2 "trust me, skip the
check" would have moved the risk instead of removing it.

Routing is decided by INSPECTING the working copy, not by asking the caller. A
caller that could declare "this project is SQL-only" would be a caller that can
request the writable account for a project full of Python.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from datacode_common.task_protocol import TaskResult, TaskStatus

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Task kinds, matching conf/isolation-profiles.yaml.
KIND_PLAN = "sqlmesh.plan"
KIND_PLAN_WITH_PYTHON = "sqlmesh.plan_with_python"
KIND_APPLY = "sqlmesh.apply"
KIND_RENDER_PROJECT = "sqlmesh.render_project"

# Parameter carrying the rendered project's location into phase 2.
PARAM_PROJECT_SUBDIR = "project_subdir"

# Directories whose ``.py`` files execute when a Context loads. Mirrors the
# runner's own list; kept here so the routing decision does not require starting a
# sandbox to discover which route to take.
_PYTHON_CODE_DIRS = ("models", "macros")

# SQLMesh project sub-directory of a working copy.
_SQL_WAREHOUSE_DIR = "sql-warehouse"


@dataclass(frozen=True)
class WriteRoute:
    """How an operation on one working copy must be executed.

    Attributes:
        kinds: Task kinds to run, in order. One entry for an SQL-only project,
            two for the split.
        reason: Why this route was chosen, for the log and the audit trail.
        python_files: The files that forced the split, empty when none.
    """

    kinds: tuple[str, ...]
    reason: str
    python_files: tuple[str, ...] = ()

    @property
    def is_split(self) -> bool:
        return len(self.kinds) > 1


def project_python_files(workspace_path: str) -> tuple[str, ...]:
    """List the project files that make a Context execute user code.

    A filesystem inspection rather than a Context load: deciding the route must
    not itself execute the code whose execution is being routed.
    """
    root = Path(workspace_path)
    warehouse = root / _SQL_WAREHOUSE_DIR
    project = warehouse if warehouse.is_dir() else root

    found: list[str] = []
    for directory in _PYTHON_CODE_DIRS:
        candidate = project / directory
        if not candidate.is_dir():
            continue
        for path in sorted(candidate.rglob("*.py")):
            try:
                if path.name == "__init__.py" and path.stat().st_size == 0:
                    continue
                found.append(str(path.relative_to(project)))
            except OSError:
                # A file that vanished mid-scan cannot execute either.
                continue
    return tuple(found)


def route_for_apply(workspace_path: str) -> WriteRoute:
    """Decide how to execute an ``apply`` on this working copy."""
    python_files = project_python_files(workspace_path)
    if not python_files:
        return WriteRoute(
            kinds=(KIND_APPLY,),
            reason="the project is SQL-only, so the writable account is safe in L2",
        )
    return WriteRoute(
        kinds=(KIND_RENDER_PROJECT, KIND_APPLY),
        reason=(
            f"the project contains host-language code ({len(python_files)} file(s)), "
            "so it is rendered in L3-base with a read-only account and applied in "
            "L2 with the writable one (Q5)"
        ),
        python_files=python_files,
    )


def route_for_plan(workspace_path: str) -> WriteRoute:
    """Decide how to execute a ``plan``.

    A plan does not write, so no split is needed — only the right profile. A
    project with Python goes to the kind whose profile grants a read-only account
    and expects user code.
    """
    python_files = project_python_files(workspace_path)
    if not python_files:
        return WriteRoute(
            kinds=(KIND_PLAN,), reason="SQL-only project: plan runs in L2"
        )
    return WriteRoute(
        kinds=(KIND_PLAN_WITH_PYTHON,),
        reason="project contains host-language code: plan runs in L3-base",
        python_files=python_files,
    )


async def submit_apply(
    submitter: Any,
    workspace_path: str,
    *,
    environment: str = "",
    tenant_id: str = "",
    workspace_id: str = "",
    gateway: str = "",
    actor_user_id: str = "",
    timeout_seconds: int = 0,
    params: Optional[Mapping[str, Any]] = None,
) -> TaskResult:
    """Apply a project, splitting the work when Q5 requires it.

    Args:
        submitter: The :class:`~src.tasks.submitter.TaskSubmitter`. Injected rather
            than fetched so this module stays testable without a task plane.
        workspace_path: Working copy to act on.
        environment: Target SQLMesh environment.
        tenant_id: Owning tenant, for audit and quota attribution.
        workspace_id: Owning workspace, for audit.
        gateway: Warehouse gateway, empty for the project default.
        actor_user_id: The user the work is attributed to.
        timeout_seconds: Budget for EACH phase. The split runs two sandboxes, so a
            single shared budget would make the write phase inherit whatever the
            render phase left — which is how a large project's apply gets cut off
            mid-write.
        params: Extra kind parameters.

    Returns:
        The final phase's result. A failed render phase is returned as-is: nothing
        was written, and reporting it as an apply failure would misdescribe it.
    """
    route = await asyncio.to_thread(route_for_apply, workspace_path)
    base_params = dict(params or {})

    logger.info(
        "sqlmesh_apply_routed",
        workspace_path=workspace_path,
        environment=environment,
        kinds=list(route.kinds),
        reason=route.reason,
        python_files=list(route.python_files[:10]),
    )

    if not route.is_split:
        return await submitter.submit(
            KIND_APPLY,
            workspace_path,
            environment=environment,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            params=base_params,
            gateway=gateway,
            actor_user_id=actor_user_id,
            timeout_seconds=timeout_seconds,
        )

    # ── phase 1: render in L3-base, read-only account ──────────────────
    rendered = await submitter.submit(
        KIND_RENDER_PROJECT,
        workspace_path,
        environment=environment,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        params=base_params,
        gateway=gateway,
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )
    if not rendered.succeeded:
        logger.warning(
            "sqlmesh_render_phase_failed",
            workspace_path=workspace_path,
            error_code=rendered.error_code,
        )
        return rendered

    project_subdir = str(rendered.result.get("rendered_project") or "")
    if not project_subdir:
        # Without it, phase 2 would apply the ORIGINAL project — which contains
        # the Python this whole split exists to keep away from a writable account.
        logger.error(
            "sqlmesh_render_phase_incomplete",
            workspace_path=workspace_path,
            hint="the render phase reported no rendered_project path",
        )
        return TaskResult.failure(
            rendered.task_id,
            "task_failed",
            "the render phase did not report where it wrote the rendered project, "
            "so the write phase would act on the original one; refusing",
            status=TaskStatus.FAILED,
        )

    remaining = rendered.result.get("python_code_remaining") or []
    if remaining:
        # The render phase's own evidence that its output is SQL-only. If it is
        # not, phase 2's check would refuse anyway — failing here says why.
        logger.error(
            "sqlmesh_render_phase_left_python",
            workspace_path=workspace_path,
            files=list(remaining)[:10],
        )
        return TaskResult.failure(
            rendered.task_id,
            "task_failed",
            "the rendered project still contains host-language code "
            f"({', '.join(str(f) for f in list(remaining)[:3])}); the write phase "
            "cannot run with a writable account against it",
            status=TaskStatus.FAILED,
        )

    # ── phase 2: apply the rendered project in L2, writable account ─────
    write_params = dict(base_params)
    write_params[PARAM_PROJECT_SUBDIR] = project_subdir

    logger.info(
        "sqlmesh_write_phase_start",
        workspace_path=workspace_path,
        project_subdir=project_subdir,
        models_rendered=rendered.result.get("model_count"),
    )
    result = await submitter.submit(
        KIND_APPLY,
        workspace_path,
        environment=environment,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        params=write_params,
        gateway=gateway,
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )
    # Surfaced in the result so a caller (and the audit record) can see that two
    # sandboxes were involved, rather than inferring it from two task rows.
    if result.succeeded:
        enriched = dict(result.result)
        enriched["render_phase"] = {
            "task_id": rendered.task_id,
            "models_rendered": rendered.result.get("model_count"),
            "project_subdir": project_subdir,
        }
        return TaskResult(
            task_id=result.task_id,
            status=result.status,
            result=enriched,
            duration_ms=result.duration_ms + rendered.duration_ms,
            exit_code=result.exit_code,
        )
    return result


__all__ = [
    "KIND_APPLY",
    "KIND_PLAN",
    "KIND_PLAN_WITH_PYTHON",
    "KIND_RENDER_PROJECT",
    "PARAM_PROJECT_SUBDIR",
    "WriteRoute",
    "project_python_files",
    "route_for_apply",
    "route_for_plan",
    "submit_apply",
]
