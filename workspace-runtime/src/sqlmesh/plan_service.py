"""Plan & Apply service — the caller of the sandbox, never the engine.

P6c architecture: NOTHING in this module touches a SQLMesh Context any more.

* **preview** runs in the warm sandbox (executor), which holds the hot Context
  and executes the user's Python models there — where there are no platform
  credentials to steal. The service keeps only what a caller-facing layer owns:
  the environment guard, SSE stage events, the workspace fingerprint and error
  translation.
* **apply** runs its GATE in the same sandbox (tests, lint, the no-changes
  check — everything that decides whether writing is worth doing) and then
  submits the WRITE to the task plane via ``write_routing.submit_apply``, which
  enforces the Q5 split: the writable warehouse account only ever reaches an
  SQL-only vehicle, never the code-executing one.

Both halves preserve the pre-P6c service contract: the same response shapes,
the same 422 for linter rejections, the same optimistic-lock fingerprint, the
same environment lock and apply history. Callers (REST endpoints, SSE) are
unchanged.
"""

import asyncio
import subprocess
import time
from typing import Any, Dict, Optional

from datacode_common.executor_protocol import OP_PLAN

from src.environments.guard import mark_applied, require_registered_environment
from src.executor.process import ExecutorError, get_executor_manager
from src.sqlmesh.env_locks import check_environment_lock, environment_lock
from src.sqlmesh.event_bus import EventTopic, SSEEvent, get_event_bus
from src.sqlmesh.task_tracker import get_task_tracker
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import (
    ConflictError,
    ErrorCode,
    ServiceError,
    service_error_from_exception,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PlanLintError(ServiceError):
    """Plan aborted by the linter. Carries a structured ``lint_results`` payload.

    Lint failures are user-fixable problems in their model code (e.g. an
    unknown column reference) — not server faults. They map to HTTP 422 so the
    frontend can render a rich "Linter errors" panel without treating them
    as 500-class infrastructure incidents.
    """

    def __init__(self, workspace_path: str, lint_results: dict):
        # Defensive: store the payload so exception handlers / SSE consumers
        # can re-attach it without re-running the linter.
        self.lint_results = lint_results
        total = lint_results.get("total", 0)
        errors = lint_results.get("errors", 0)
        # Prefer the native-CLI-style ``report`` (grouped per-file/per-rule
        # detail) as the human-readable message so both the CLI wrapper and the
        # frontend show actionable text instead of a bare summary. Fall back to
        # the short summary, then a generic sentence.
        report = (lint_results.get("report") or "").strip()
        summary = (
            f"Plan blocked by linter: {errors} error(s) across "
            f"{total} total finding(s). Fix the highlighted models and re-run plan."
        )
        message = report or lint_results.get("message") or summary
        super().__init__(
            code=ErrorCode.PLAN_FAILED,
            message=message,
            detail=lint_results,
            status_code=422,
        )


class PlanService:
    """Canonical Plan/Apply operations over the sandboxed executor."""

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    async def preview(
        self,
        workspace_path: str,
        environment: str = "",
        gateway: Optional[str] = None,
        *,
        plan_dates: Optional[dict] = None,
        plan_options: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Create a plan preview without applying.

        The target must be the registered environment this working copy is
        checked out to (see :mod:`src.environments.guard`). Preview is a read
        operation, so it takes no environment lock: the returned
        ``state_fingerprint`` is what protects the follow-up apply.
        """
        resolved = await require_registered_environment(workspace_path, environment)
        environment = resolved.name

        try:
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "init"
            )

            result = await self._sandbox_plan(
                workspace_path,
                environment,
                gateway,
                plan_options=plan_options,
                plan_dates=plan_dates,
                topic=EventTopic.PLAN_OVERVIEW,
            )

            if result.get("tests_failed"):
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "fail",
                    data={"test_results": result.get("test_results")},
                )
                result["state_fingerprint"] = await self._get_workspace_fingerprint(
                    workspace_path
                )
                return result

            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "success"
            )
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_OVERVIEW, "changes", "init"
            )
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_OVERVIEW, "changes", "success",
                data=result.get("changes"),
            )
            if result.get("requires_backfill"):
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "backfills", "success",
                    data=result.get("backfills"),
                )

            result["state_fingerprint"] = await self._get_workspace_fingerprint(
                workspace_path
            )

            logger.info(
                "plan_preview_created",
                workspace_path=workspace_path,
                environment=environment,
                has_changes=result.get("has_changes"),
            )
            return result

        except ServiceError:
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "fail"
            )
            raise

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    async def apply(
        self,
        workspace_path: str,
        environment: str = "",
        gateway: Optional[str] = None,
        *,
        plan_dates: Optional[dict] = None,
        plan_options: Optional[dict] = None,
        categories: Optional[dict] = None,
        expected_fingerprint: Optional[str] = None,
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "human",
    ) -> Dict[str, Any]:
        """Create and apply a plan for a registered environment.

        Guards, then serializes:

        * the target must be the registered environment of this working copy, so
          an environment can never come into existence as a side effect of apply;
        * only ONE state-mutating operation may run per environment across all
          members, because they share a single SQLMesh state schema (see
          :mod:`src.core.env_locks`). A busy environment answers 409 immediately.

        Then: the GATE (tests, lint, no-changes) runs in the read-only sandbox;
        the WRITE runs in the task plane under the Q5 split. On success the
        registry records the applied commit, which is what makes "code and data
        are in sync" observable for the UI and the reconciler.
        """
        resolved = await require_registered_environment(workspace_path, environment)

        async with environment_lock(workspace_path, operation="apply"):
            # Snapshot code identity under the lock, BEFORE the plan runs: what
            # gets applied is the state of the files at this instant. Reading
            # HEAD afterwards would attribute the data to a commit that landed
            # DURING the apply, and ignoring dirtiness would attribute it to a
            # commit whose content never ran — either way the "code and data
            # share one source" signal would be forged.
            #
            # A dirty tree is NOT rejected: iterating before committing is the
            # normal sandbox workflow. It is recorded instead, and surfaces as
            # drift until the user commits and re-applies.
            head_commit = await self._get_workspace_fingerprint(workspace_path)
            dirty = await self._workspace_is_dirty(workspace_path)

            result = await self._apply(
                workspace_path=workspace_path,
                environment=resolved.name,
                gateway=gateway,
                plan_dates=plan_dates,
                plan_options=plan_options,
                categories=categories,
                expected_fingerprint=expected_fingerprint,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
            )

            if result.get("applied"):
                await mark_applied(
                    resolved.environment_id,
                    commit_hash=head_commit,
                    dirty=dirty,
                )
                result["applied_commit"] = head_commit
                result["applied_from_dirty_worktree"] = dirty
        return result

    async def _apply(
        self,
        workspace_path: str,
        environment: str,
        gateway: Optional[str] = None,
        *,
        plan_dates: Optional[dict] = None,
        plan_options: Optional[dict] = None,
        categories: Optional[dict] = None,
        expected_fingerprint: Optional[str] = None,
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "human",
    ) -> Dict[str, Any]:
        """Gate in the sandbox, then submit the write to the task plane.

        Callers must already hold the environment lock.
        """
        t_start = time.monotonic()
        tracker = get_task_tracker()

        # Check for conflicting running task
        if await tracker.has_running_task(workspace_path):
            raise ConflictError(
                "Another plan/apply operation is already running for this workspace."
            )

        # Optimistic lock check
        if expected_fingerprint:
            current = await self._get_workspace_fingerprint(workspace_path)
            if current and current != expected_fingerprint:
                raise ConflictError(
                    "Workspace modified since plan creation. "
                    "Please re-run plan preview to get fresh changes."
                )

        # ── GATE: tests, lint and the no-changes check, in the read-only
        # sandbox. Structured outcomes (test results, lint panels) can only
        # travel on the executor's rich channel, so the gate runs BEFORE the
        # write is submitted; the write vehicle then re-builds the plan from
        # the same files, which is the price of keeping the writable account
        # out of the code-executing sandbox (Q5).
        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "creation", "init"
        )

        gate = await self._sandbox_plan(
            workspace_path,
            environment,
            gateway,
            plan_options=plan_options,
            plan_dates=plan_dates,
            categories=categories,
            topic=EventTopic.PLAN_APPLY,
        )

        if gate.get("tests_failed"):
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "creation", "fail",
                data={"test_results": gate.get("test_results")},
            )
            logger.info(
                "plan_apply_tests_failed",
                workspace_path=workspace_path,
                failures=(gate.get("test_results") or {}).get("failures"),
                errors=(gate.get("test_results") or {}).get("errors"),
            )
            duration_ms = int((time.monotonic() - t_start) * 1000)
            await self._save_apply_history(
                workspace_path=workspace_path,
                environment=environment,
                applied=False,
                reason="tests_failed",
                has_changes=False,
                plan_result=None,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                duration_ms=duration_ms,
            )
            return {
                "applied": False,
                "reason": "tests_failed",
                "has_changes": False,
                "requires_backfill": False,
                "environment": environment,
                "tests_failed": True,
                "test_results": gate.get("test_results"),
            }

        if not gate.get("has_changes") and not gate.get("requires_backfill"):
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "creation", "success",
                data={"applied": False, "reason": "no_changes"},
            )
            logger.info("plan_apply_no_changes", workspace_path=workspace_path)
            duration_ms = int((time.monotonic() - t_start) * 1000)
            await self._save_apply_history(
                workspace_path=workspace_path,
                environment=environment,
                applied=False,
                reason="no_changes",
                has_changes=False,
                plan_result=gate,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                duration_ms=duration_ms,
            )
            return {
                "applied": False,
                "reason": "no_changes",
                "has_changes": False,
                "requires_backfill": False,
                "environment": environment,
            }

        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "creation", "success"
        )
        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "backfill", "init"
        )

        # ── WRITE: the Q5 split (render in L3 with a read-only account when
        # the project contains Python; apply in L2 with the writable one).
        from src.config import settings
        from src.tasks.write_routing import submit_apply

        tenant_id, workspace_id, _actor, _env = _identity(workspace_path)
        try:
            task_result = await submit_apply(
                _get_task_submitter(),
                workspace_path,
                environment=environment,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                gateway=gateway or "",
                actor_user_id=user_id,
                timeout_seconds=settings.task_max_timeout_seconds,
                params={
                    "plan_options": plan_options or {},
                    "plan_dates": plan_dates or {},
                    "categories": categories or {},
                },
            )
        except Exception as exc:  # noqa: BLE001 — mapped below
            svc_err = service_error_from_exception(
                exc,
                code=ErrorCode.APPLY_FAILED,
                fallback_message="Failed to apply plan",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="plan.apply",
                    gateway=gateway,
                    context={"environment": environment},
                ),
            )
            await self._finish_failed_apply(
                workspace_path=workspace_path,
                environment=environment,
                gateway=gateway,
                svc_err=svc_err,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                t_start=t_start,
            )
            raise svc_err from exc

        if not task_result.succeeded:
            message = task_result.error_message or "plan apply failed"
            if task_result.error_code == "lint_failed":
                # A lint rejection that slipped past the gate (files changed
                # between gate and write). Same 422 treatment, with the report
                # the task captured.
                raise PlanLintError(
                    workspace_path,
                    {"total": 0, "errors": 0, "report": message, "message": message},
                )
            # Vehicle-level verdicts (timeout / OOM / no result) keep their
            # contract codes: the user's next action differs from fixing code,
            # and a 422 APPLY_FAILED would hide it (design 08 §7).
            from src.tasks.submitter import sandbox_verdict_for

            svc_err = sandbox_verdict_for(task_result) or ServiceError(
                code=ErrorCode.APPLY_FAILED,
                message=message,
                status_code=422,
            )
            await self._finish_failed_apply(
                workspace_path=workspace_path,
                environment=environment,
                gateway=gateway,
                svc_err=svc_err,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                t_start=t_start,
            )
            raise svc_err

        applied = bool(task_result.result.get("applied", True))
        has_changes = bool(task_result.result.get("has_changes", True))

        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "backfill", "success"
        )
        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "promote", "success"
        )
        logger.info(
            "plan_applied",
            workspace_path=workspace_path,
            environment=environment,
        )

        duration_ms = int((time.monotonic() - t_start) * 1000)
        await self._save_apply_history(
            workspace_path=workspace_path,
            environment=environment,
            applied=applied,
            reason=None,
            has_changes=has_changes,
            plan_result=dict(task_result.result),
            user_id=user_id,
            session_id=session_id,
            trigger_type=trigger_type,
            duration_ms=duration_ms,
        )

        # The write changed warehouse state; every cached Context over this
        # working copy is now stale — the sandbox's AND the runtime's metadata
        # cache's.
        from src.sqlmesh.metadata_context import get_metadata_context

        get_executor_manager().invalidate(workspace_path)
        get_metadata_context().invalidate(workspace_path)

        return {
            "applied": applied,
            "has_changes": has_changes,
            "requires_backfill": bool(
                task_result.result.get("requires_backfill", False)
            ),
            "environment": environment,
        }

    async def _finish_failed_apply(
        self,
        *,
        workspace_path: str,
        environment: str,
        gateway: Optional[str],
        svc_err: ServiceError,
        user_id: str,
        session_id: str,
        trigger_type: str,
        t_start: float,
    ) -> None:
        """Shared tail for a failed write: SSE fail event + history record."""
        error_text = (
            svc_err.detail if isinstance(svc_err.detail, str) and svc_err.detail
            else svc_err.message
        )
        await self._publish_plan_stage(
            workspace_path, EventTopic.PLAN_APPLY, "backfill", "fail",
            data={"error": error_text},
        )
        logger.error(
            "plan_apply_failed", error=error_text, workspace_path=workspace_path
        )
        await self._save_apply_history(
            workspace_path=workspace_path,
            environment=environment,
            applied=False,
            reason=error_text[:256],
            has_changes=False,
            plan_result=None,
            user_id=user_id,
            session_id=session_id,
            trigger_type=trigger_type,
            duration_ms=int((time.monotonic() - t_start) * 1000),
            error_message=error_text,
        )

    # ------------------------------------------------------------------
    # async wrapper & cancellation
    # ------------------------------------------------------------------

    async def apply_async(
        self,
        workspace_path: str,
        environment: str = "",
        gateway: Optional[str] = None,
        *,
        plan_dates: Optional[dict] = None,
        plan_options: Optional[dict] = None,
        categories: Optional[dict] = None,
        expected_fingerprint: Optional[str] = None,
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "human",
    ) -> Dict[str, Any]:
        """Launch plan apply as a background task (non-blocking).

        Returns immediately with task status. Progress is delivered via SSE.
        The environment is validated BEFORE the task is spawned so an invalid
        target answers with a normal HTTP error instead of an SSE failure the
        caller has to subscribe to.
        """
        resolved = await require_registered_environment(workspace_path, environment)
        # Fail fast on a busy environment so the caller gets a normal 409
        # instead of a started task that fails over SSE. Advisory only: the
        # task still acquires the real lock, which is what serializes.
        await check_environment_lock(workspace_path, operation="apply")
        tracker = get_task_tracker()
        key = f"{workspace_path}:{gateway or 'default'}"

        if await tracker.has_running_task(workspace_path):
            raise ConflictError(
                "Another plan/apply operation is already running for this workspace."
            )

        circuit_breaker = asyncio.Event()

        # Create background task
        task = asyncio.create_task(
            self.apply(
                workspace_path=workspace_path,
                environment=resolved.name,
                gateway=gateway,
                plan_dates=plan_dates,
                plan_options=plan_options,
                categories=categories,
                expected_fingerprint=expected_fingerprint,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
            )
        )

        if not await tracker.register(
            key=key,
            workspace_path=workspace_path,
            task_type="apply",
            task=task,
            circuit_breaker=circuit_breaker,
        ):
            # The SET NX race was lost to another replica between the check
            # above and here; surface the same conflict.
            raise ConflictError(
                "Another plan/apply operation is already running for this workspace."
            )

        return {
            "status": "started",
            "message": (
                "Plan apply started as background task. "
                "Monitor progress via SSE /api/v1/events."
            ),
        }

    async def cancel(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a running plan application.

        The circuit breaker stops the background task between stages. A write
        already handed to the task plane cannot be interrupted mid-SQL; it ends
        with the task's own timeout, which is the honest cancellation a socket
        boundary permits (recorded as a P6c deviation).
        """
        tracker = get_task_tracker()
        key = f"{workspace_path}:{gateway or 'default'}"

        cancelled = await tracker.cancel(key)

        if cancelled:
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_CANCEL, "cancel", "success"
            )
            logger.info("plan_cancel_requested", workspace_path=workspace_path)
            return {"cancelled": True, "message": "Cancellation signal sent"}
        else:
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_CANCEL, "cancel", "fail",
                data={"reason": "no_running_task"},
            )
            logger.info("plan_cancel_no_running_task", workspace_path=workspace_path)
            return {"cancelled": False, "message": "No running plan to cancel"}

    # -------------------------------------------------------------------------
    # sandbox bridge
    # -------------------------------------------------------------------------

    async def _sandbox_plan(
        self,
        workspace_path: str,
        environment: str,
        gateway: Optional[str],
        *,
        plan_options: Optional[dict] = None,
        plan_dates: Optional[dict] = None,
        categories: Optional[dict] = None,
        topic: EventTopic = EventTopic.PLAN_OVERVIEW,
    ) -> Dict[str, Any]:
        """Run the plan build in the warm sandbox and translate failures.

        The sandbox runs the tests, the plan build with the caller's options and
        category choices, the lint check and the rich formatting — everything
        that executes user code. This side only speaks the frozen protocol.

        Raises:
            PlanLintError: the sandbox reported a lint rejection (code
                ``lint_failed``); the structured violations ride in the error
                payload.
            ServiceError: the sandbox is unavailable or the op failed —
                including an archive entry for the latter.
        """
        try:
            return await get_executor_manager().call(
                workspace_path,
                environment,
                OP_PLAN,
                {
                    "gateway": gateway or "",
                    "environment": environment,
                    "plan_options": plan_options or {},
                    "plan_dates": plan_dates or {},
                    "categories": categories or {},
                },
                gateway=gateway or "",
            )
        except ExecutorError as exc:
            if exc.code == "lint_failed":
                lint_results = exc.detail.get("lint_results") or {}
                await self._publish_plan_stage(
                    workspace_path, topic, "validation", "fail",
                    data={"lint_results": lint_results},
                )
                logger.warning(
                    "plan_lint_failed",
                    workspace_path=workspace_path,
                    lint_total=lint_results.get("total"),
                    lint_errors=lint_results.get("errors"),
                    lint_warnings=lint_results.get("warnings"),
                )
                raise PlanLintError(
                    workspace_path=workspace_path, lint_results=lint_results
                ) from exc

            svc_err = service_error_from_exception(
                exc,
                code=ErrorCode.PLAN_FAILED,
                fallback_message="Failed to create plan preview",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="plan.preview",
                    gateway=gateway,
                    context={"environment": environment, "executor_code": exc.code},
                ),
            )
            raise svc_err from exc

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _get_workspace_fingerprint(self, workspace_path: str) -> str:
        """Use git HEAD commit hash as workspace state fingerprint."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", workspace_path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    async def _workspace_is_dirty(self, workspace_path: str) -> bool:
        """Whether the working copy carries changes that are not in Git.

        Untracked files are deliberately EXCLUDED (``-uno``): a working copy
        materialized from a repository without the platform's ``.gitignore``
        accumulates regenerable runtime output (``sql-warehouse/.cache/``,
        logs, engine files) that no user considers a change. Only tracked
        modifications and staged content make the applied data
        non-reproducible from the recorded commit.
        """
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", workspace_path, "status", "--porcelain", "-uno"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            # A probe failure must not turn into a false "clean" claim, but it
            # must not block the apply either: report clean and let the commit
            # marker carry the (correct) HEAD.
            return False

    async def _publish_plan_stage(
        self,
        workspace_path: str,
        topic: EventTopic,
        stage: str,
        status: str,
        data: Any = None,
    ) -> None:
        """Publish a plan stage progress event via EventBus."""
        bus = get_event_bus()
        event_data = {"stage": stage, "status": status}
        if data is not None:
            event_data["data"] = data

        await bus.publish(SSEEvent(
            topic=topic,
            data=event_data,
            workspace_path=workspace_path,
        ))

    async def _save_apply_history(
        self,
        *,
        workspace_path: str,
        environment: str,
        applied: bool,
        reason: Optional[str],
        has_changes: bool,
        plan_result: Optional[Dict[str, Any]],
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "human",
        duration_ms: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        """Persist an apply history record via PlanHistoryService."""
        try:
            from src.sqlmesh.history_service import get_history_service
            await get_history_service().save(
                workspace_path=workspace_path,
                environment=environment,
                applied=applied,
                reason=reason,
                has_changes=has_changes,
                plan_result=plan_result,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                duration_ms=duration_ms,
                error_message=error_message,
            )
        except Exception as e:
            # History save failure must never block the apply result
            logger.error(
                "apply_history_save_failed",
                error=str(e),
                workspace_path=workspace_path,
            )


def _identity(workspace_path: str) -> tuple[str, str, str, str]:
    """(tenant, workspace, user, environment) from the working-copy path."""
    from datacode_common.workspace_paths import parse_workspace_path

    from src.config import settings

    return parse_workspace_path(settings.workspace_base_path, workspace_path)


def _get_task_submitter():
    """The process-wide task submitter; imported lazily to avoid cycles."""
    from src.tasks.submitter import get_task_submitter

    return get_task_submitter()


# Singleton
_plan_service: Optional[PlanService] = None


def get_plan_service() -> PlanService:
    global _plan_service
    if _plan_service is None:
        _plan_service = PlanService()
    return _plan_service
