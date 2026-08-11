"""Plan & Apply service - canonical SQLMesh plan operations.

Provides plan preview, apply, and cancel with:
- Context Pool integration
- Write lock serialization
- State fingerprint for optimistic locking (multi-user collaboration)
- Dirty flag refresh before operations
- Plan cancellation via circuit breaker
- Async task execution with SSE progress broadcasting
- Rich change/backfill details matching SQLMesh web server output

Architecture:
- Plan/Apply operations run as background asyncio.Tasks
- Progress is broadcast via EventBus to connected SSE clients
- TaskTracker enables /meta endpoint to report running status
- Circuit breaker allows cancellation mid-execution
"""

import asyncio
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from src.core.context_pool import get_context_pool
from src.core.event_bus import EventTopic, SSEEvent, get_event_bus
from src.core.format import format_lint_violations, format_plan_result, format_test_failure_summary
from src.core.task_tracker import get_task_tracker
from src.utils.errors import (
    ConflictError,
    ErrorCode,
    ServiceError,
    service_error_from_exception,
)
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

# ``LinterError`` is the typed exception SQLMesh raises when ``plan_builder.build``
# fails because one or more models violated linter rules. It is distinct from
# ``PlanError`` (no changes, conflict, etc.) and from generic ``Exception``
# (config / infra failures). Importing it at module load time keeps the import
# failures loud and obvious if the SQLMesh version ever drops the class.
try:
    from sqlmesh.utils.errors import LinterError as _LinterErrorCls
except ImportError:  # pragma: no cover - older SQLMesh fallback
    _LinterErrorCls = None  # type: ignore[assignment]

logger = get_logger(__name__)


class _FailedModelsCapturingConsole:
    """Transparent proxy around a SQLMesh console that records backfill failures.

    During ``context.apply`` SQLMesh reports the real per-model failure (e.g. a
    SQL syntax / ``ProgrammingError``) ONLY by calling
    ``console.log_failed_models(errors)`` and then raising a generic
    ``PlanError("Plan application failed.")`` that carries no detail. To recover
    the actionable cause (matching the native CLI's "Failed models" report) we
    temporarily swap the context console for this proxy: it captures the
    ``NodeExecutionFailedError`` list into a caller-provided sink while
    delegating every other attribute/method to the wrapped console, so console
    behavior for all other callers is unchanged.
    """

    def __init__(self, wrapped: Any, sink: List[Any]) -> None:
        # Bypass our own __setattr__ (which forwards to the wrapped console).
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_sink", sink)

    def log_failed_models(self, errors: Any) -> Any:
        # Capture the raw errors for later formatting, then delegate so the
        # normal console output (backend logs) still happens.
        try:
            self._sink.extend(list(errors))
        except Exception:  # never let capture break the apply flow
            pass
        return object.__getattribute__(self, "_wrapped").log_failed_models(errors)

    def __getattr__(self, name: str) -> Any:
        # Only reached when the attribute is not found on this proxy itself.
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Forward attribute writes (e.g. SQLMesh setting ``dialect``) to the
        # real console so it stays the single source of truth.
        setattr(object.__getattribute__(self, "_wrapped"), name, value)


def _build_failed_models_report(errors: List[Any]) -> str:
    """Render captured node errors as the native-CLI-style "Failed models" report.

    Reuses SQLMesh's own ``_format_node_errors`` so the per-model layout and the
    underlying error text (unwrapped from ``NodeExecutionFailedError.__cause__``)
    match exactly what ``sqlmesh plan --auto-apply`` prints on the terminal.
    """
    try:
        from sqlmesh.core.console import _format_node_errors

        formatted = _format_node_errors(errors)
        lines = ["Failed models", ""]
        for node_name, msg in formatted.items():
            # ``msg`` is already indented / newline-terminated by SQLMesh.
            lines.append(f"  {node_name}\n\n{msg.rstrip()}")
        return "\n".join(lines)
    except Exception as fmt_err:  # pragma: no cover - defensive formatting guard
        # Fall back to the raw error strings so detail is never fully lost.
        logger.warning("failed_models_report_format_failed", error=str(fmt_err))
        return "Failed models\n\n" + "\n".join(
            f"  {str(getattr(e, '__cause__', None) or e)}" for e in errors
        )


class PlanLintError(ServiceError):
    """Plan aborted by the linter. Carries a structured ``lint_results`` payload.

    Lint failures are user-fixable problems in their model code (e.g. an
    unknown column reference) — not server faults. They map to HTTP 422 so
    the frontend can render a rich "Linter errors" panel without treating
    them as 500-class infrastructure incidents.
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
    """Canonical Plan/Apply operations using SQLMesh SDK.

    Supports two modes:
    - Synchronous: Returns full result (used by plan preview)
    - Async: Returns immediately, pushes progress via SSE (used by apply)
    """

    async def preview(
        self,
        workspace_path: str,
        environment: str = "dev",
        gateway: Optional[str] = None,
        *,
        plan_dates: Optional[dict] = None,
        plan_options: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Create a plan preview without applying.

        Returns plan diff info + state fingerprint for optimistic locking.
        This runs synchronously since the frontend needs the full result.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        async with entry.write_lock:
            await pool.refresh_if_dirty(entry)

            plan_options = plan_options or {}
            plan_dates = plan_dates or {}
            skip_tests = bool(plan_options.get("skip_tests", False))

            try:
                # Publish progress: validation stage
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "init"
                )

                # Run unit tests first (unless skipped) so failures are surfaced
                # as structured results instead of a generic 500 error.
                tests_skipped = skip_tests
                tests_passed = False
                if not skip_tests:
                    test_summary = await asyncio.to_thread(
                        self._run_tests_summary, entry.context
                    )
                    if test_summary is not None and test_summary["total"] > 0:
                        if test_summary["failures"] or test_summary["errors"]:
                            # Tests failed -> return structured failure (HTTP 200).
                            await self._publish_plan_stage(
                                workspace_path, EventTopic.PLAN_OVERVIEW, "validation",
                                "fail", data={"test_results": test_summary},
                            )
                            logger.info(
                                "plan_preview_tests_failed",
                                workspace_path=workspace_path,
                                failures=test_summary["failures"],
                                errors=test_summary["errors"],
                            )
                            return {
                                "environment": environment,
                                "has_changes": False,
                                "requires_backfill": False,
                                "tests_failed": True,
                                "tests_skipped": False,
                                "tests_passed": False,
                                "test_results": test_summary,
                                "state_fingerprint": await self._get_workspace_fingerprint(
                                    workspace_path
                                ),
                            }
                        tests_passed = True

                # Build the plan with skip_tests=True since tests already ran above.
                build_options = {**plan_options, "skip_tests": True}
                plan_builder = await asyncio.to_thread(
                    self._build_plan,
                    entry.context,
                    environment=environment,
                    plan_options=build_options,
                    plan_dates=plan_dates,
                )

                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "success"
                )

                # Publish progress: changes stage
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "changes", "init"
                )

                plan = await asyncio.to_thread(plan_builder.build)
                result = format_plan_result(plan, context=entry.context)

                # Set category choices if provided
                categories = plan_options.get("categories")
                if categories and plan.context_diff.has_changes:
                    from sqlmesh.core.snapshot.definition import SnapshotChangeCategory
                    for new, _ in plan.context_diff.modified_snapshots.values():
                        if plan.is_new_snapshot(new) and new.name in categories:
                            category = SnapshotChangeCategory[categories[new.name]]
                            plan_builder.set_choice(new, category)
                    plan = await asyncio.to_thread(plan_builder.build)
                    result = format_plan_result(plan, context=entry.context)

                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "changes", "success",
                    data=result.get("changes"),
                )

                # Publish progress: backfills stage
                if plan.requires_backfill:
                    await self._publish_plan_stage(
                        workspace_path, EventTopic.PLAN_OVERVIEW, "backfills", "success",
                        data=result.get("backfills"),
                    )

                # Annotate test execution status for the frontend.
                result["tests_skipped"] = tests_skipped
                result["tests_passed"] = tests_passed
                result["tests_failed"] = False
                result["state_fingerprint"] = await self._get_workspace_fingerprint(workspace_path)

                logger.info(
                    "plan_preview_created",
                    workspace_path=workspace_path,
                    environment=environment,
                    has_changes=plan.has_changes,
                )
                return result

            except ServiceError:
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "fail"
                )
                raise
            except Exception as e:
                # Linter rejected the plan: this is a USER-FIXABLE error in their
                # model code, not a server fault. Return a structured 422 with
                # the per-file/per-rule violations so the UI can render actionable
                # diagnostics instead of a generic 500. The ``_LinterErrorCls``
                # guard keeps us working even on older SQLMesh builds that
                # don't expose ``LinterError`` (we fall through to the generic
                # 500 path there).
                if _LinterErrorCls is not None and isinstance(e, _LinterErrorCls):
                    lint_results = await asyncio.to_thread(
                        self._collect_lint_violations, entry.context
                    )
                    await self._publish_plan_stage(
                        workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "fail",
                        data={"lint_results": lint_results},
                    )
                    logger.warning(
                        "plan_preview_lint_failed",
                        workspace_path=workspace_path,
                        lint_total=lint_results["total"],
                        lint_errors=lint_results["errors"],
                        lint_warnings=lint_results["warnings"],
                    )
                    raise PlanLintError(
                        workspace_path=workspace_path, lint_results=lint_results
                    )

                svc_err = service_error_from_exception(
                    e,
                    code=ErrorCode.PLAN_FAILED,
                    fallback_message="Failed to create plan preview",
                    archive=ErrorArchiveContext(
                        workspace_path=workspace_path,
                        operation="plan.preview",
                        gateway=gateway,
                        context={"environment": environment},
                    ),
                )
                error_text = svc_err.detail if isinstance(svc_err.detail, str) and svc_err.detail else svc_err.message
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_OVERVIEW, "validation", "fail",
                    data={"error": error_text},
                )
                logger.error("plan_preview_failed", error=error_text, workspace_path=workspace_path)
                raise svc_err from e

    async def apply(
        self,
        workspace_path: str,
        environment: str = "dev",
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
        """Create and apply a plan.

        If expected_fingerprint is provided, verifies workspace hasn't changed
        since the plan preview (optimistic locking for multi-user).

        After apply (success or failure), persists a history record for audit.
        """
        t_start = time.monotonic()
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        tracker = get_task_tracker()
        key = f"{workspace_path}:{gateway or 'default'}"

        # Check for conflicting running task
        if tracker.has_running_task(workspace_path):
            raise ConflictError(
                "Another plan/apply operation is already running for this workspace."
            )

        async with entry.write_lock:
            # Optimistic lock check
            if expected_fingerprint:
                current = await self._get_workspace_fingerprint(workspace_path)
                if current and current != expected_fingerprint:
                    raise ConflictError(
                        "Workspace modified since plan creation. "
                        "Please re-run plan preview to get fresh changes."
                    )

            await pool.refresh_if_dirty(entry)

            plan_options = plan_options or {}
            plan_dates = plan_dates or {}
            skip_tests = bool(plan_options.get("skip_tests", False))

        # Setup circuit breaker for cancellation
        circuit_breaker = asyncio.Event()

        # Sink for per-model backfill failures captured from the context console
        # (see _FailedModelsCapturingConsole). Declared here so the except block
        # can read it after `context.apply` raises the generic PlanError.
        failed_models_sink: List[Any] = []

        try:
            # Publish progress: creation stage
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "creation", "init"
            )

            # Run unit tests first (unless skipped); abort apply on failure
            # and return structured results so the UI can render them.
            if not skip_tests:
                test_summary = await asyncio.to_thread(
                    self._run_tests_summary, entry.context
                )
                if test_summary is not None and test_summary["total"] > 0 and (
                    test_summary["failures"] or test_summary["errors"]
                ):
                    await self._publish_plan_stage(
                        workspace_path, EventTopic.PLAN_APPLY, "creation", "fail",
                        data={"test_results": test_summary},
                    )
                    logger.info(
                        "plan_apply_tests_failed",
                        workspace_path=workspace_path,
                        failures=test_summary["failures"],
                        errors=test_summary["errors"],
                    )

                    # Save history record for the failed apply
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
                        "test_results": test_summary,
                    }

            # Build plan with skip_tests=True since tests already ran above.
            build_options = {**plan_options, "skip_tests": True}
            plan_builder = await asyncio.to_thread(
                self._build_plan,
                entry.context,
                environment=environment,
                plan_options=build_options,
                plan_dates=plan_dates,
                categories=categories,
            )
            plan = await asyncio.to_thread(plan_builder.build)

            if not plan.has_changes and not plan.requires_backfill:
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_APPLY, "creation", "success",
                    data={"applied": False, "reason": "no_changes"},
                )
                logger.info("plan_apply_no_changes", workspace_path=workspace_path)

                # Save history record (plan_result may be None since no changes)
                plan_result = format_plan_result(plan, context=entry.context)
                duration_ms = int((time.monotonic() - t_start) * 1000)
                await self._save_apply_history(
                    workspace_path=workspace_path,
                    environment=environment,
                    applied=False,
                    reason="no_changes",
                    has_changes=False,
                    plan_result=plan_result,
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

            # Publish progress: backfill stage
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "backfill", "init"
            )

            # Apply the plan with circuit breaker. Temporarily wrap the
            # context console so per-model backfill failures are captured:
            # SQLMesh only exposes them via console.log_failed_models before
            # raising a detail-less PlanError.
            original_console = entry.context.console
            entry.context.console = _FailedModelsCapturingConsole(
                original_console, failed_models_sink
            )
            try:
                await asyncio.to_thread(
                    entry.context.apply, plan, circuit_breaker.is_set
                )
            finally:
                entry.context.console = original_console

            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "backfill", "success"
            )

            # Publish progress: promote stage
            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "promote", "success"
            )

            logger.info(
                "plan_applied",
                workspace_path=workspace_path,
                environment=environment,
            )

            # Save history record with full plan result
            plan_result = format_plan_result(plan, context=entry.context)
            duration_ms = int((time.monotonic() - t_start) * 1000)
            await self._save_apply_history(
                workspace_path=workspace_path,
                environment=environment,
                applied=True,
                reason=None,
                has_changes=plan.has_changes,
                plan_result=plan_result,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                duration_ms=duration_ms,
            )

            await pool.invalidate(workspace_path, gateway)

            return {
                "applied": True,
                "has_changes": plan.has_changes,
                "requires_backfill": plan.requires_backfill,
                "environment": environment,
            }

        except ConflictError:
            raise
        except PlanLintError:
            # PlanLintError already carries its history + stage side effects
            # upstream; just re-raise so the global handler renders 422.
            raise
        except Exception as e:
            # Linter rejection during apply: same treatment as preview —
            # convert to a structured 422 so the UI shows per-file errors.
            if _LinterErrorCls is not None and isinstance(e, _LinterErrorCls):
                lint_results = await asyncio.to_thread(
                    self._collect_lint_violations, entry.context
                )
                await self._publish_plan_stage(
                    workspace_path, EventTopic.PLAN_APPLY, "creation", "fail",
                    data={"lint_results": lint_results},
                )
                logger.warning(
                    "plan_apply_lint_failed",
                    workspace_path=workspace_path,
                    lint_total=lint_results["total"],
                    lint_errors=lint_results["errors"],
                )
                duration_ms = int((time.monotonic() - t_start) * 1000)
                await self._save_apply_history(
                    workspace_path=workspace_path,
                    environment=environment,
                    applied=False,
                    reason="lint_failed",
                    has_changes=False,
                    plan_result=None,
                    user_id=user_id,
                    session_id=session_id,
                    trigger_type=trigger_type,
                    duration_ms=duration_ms,
                    error_message=lint_results.get("message", "Lint failed"),
                )
                raise PlanLintError(
                    workspace_path=workspace_path, lint_results=lint_results
                )

            # Non-linter failure. Translate to a ServiceError that keeps the
            # actionable reason visible. Backfill failures are the common
            # case: SQLMesh reports each failed model via the console
            # (captured in ``failed_models_sink``) and then raises a
            # detail-less PlanError("Plan application failed."). We rebuild
            # the native "Failed models" report from the captured errors so
            # the CLI/frontend show the exact failing model + cause (e.g. a
            # SQL syntax error) instead of the generic wrapper. Non-backfill
            # faults fall through to the shared translation helper.
            if failed_models_sink:
                report = _build_failed_models_report(failed_models_sink)
                plan_msg = str(e).strip() or "Plan application failed."
                svc_err = ServiceError(
                    code=ErrorCode.APPLY_FAILED,
                    # Combine per-model detail with the plan-level summary,
                    # mirroring the native CLI output ordering.
                    message=f"{report}\n\n{plan_msg}",
                    status_code=422,
                )
                # Archive the reconstructed backfill failure so the agent can
                # see which model(s) failed and why. ``service_error_from_exception``
                # is not used here because we already built a rich message from
                # the captured per-model errors, but archiving is still valuable.
                try:
                    from src.utils.error_archive import ErrorArchive

                    error_id = ErrorArchive.record(
                        exc=e,
                        service_error=svc_err,
                        archive_context=ErrorArchiveContext(
                            workspace_path=workspace_path,
                            operation="plan.apply",
                            gateway=gateway,
                            context={
                                "environment": environment,
                                "failed_models": [
                                    str(getattr(fe, "__cause__", None) or fe)
                                    for fe in failed_models_sink
                                ],
                            },
                        ),
                    )
                    svc_err.error_log_id = error_id
                except Exception:
                    logger.warning("plan_apply_archive_failed", exc_info=True)
            else:
                svc_err = service_error_from_exception(
                    e,
                    code=ErrorCode.APPLY_FAILED,
                    fallback_message="Failed to apply plan",
                    archive=ErrorArchiveContext(
                        workspace_path=workspace_path,
                        operation="plan.apply",
                        gateway=gateway,
                        context={"environment": environment},
                    ),
                )
            # Prefer the human-readable message for logs / history / SSE.
            error_text = svc_err.detail if isinstance(svc_err.detail, str) and svc_err.detail else svc_err.message

            await self._publish_plan_stage(
                workspace_path, EventTopic.PLAN_APPLY, "backfill", "fail",
                data={"error": error_text},
            )
            logger.error("plan_apply_failed", error=error_text, workspace_path=workspace_path)

            # Save history record for the failed apply
            duration_ms = int((time.monotonic() - t_start) * 1000)
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
                duration_ms=duration_ms,
                error_message=error_text,
            )

            raise svc_err from e

    async def apply_async(
        self,
        workspace_path: str,
        environment: str = "dev",
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
        Used when the frontend prefers async execution with real-time updates.
        """
        tracker = get_task_tracker()
        key = f"{workspace_path}:{gateway or 'default'}"

        if tracker.has_running_task(workspace_path):
            raise ConflictError(
                "Another plan/apply operation is already running for this workspace."
            )

        circuit_breaker = asyncio.Event()

        # Create background task
        task = asyncio.create_task(
            self.apply(
                workspace_path=workspace_path,
                environment=environment,
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

        tracker.register(
            key=key,
            workspace_path=workspace_path,
            task_type="apply",
            task=task,
            circuit_breaker=circuit_breaker,
        )

        return {
            "status": "started",
            "message": "Plan apply started as background task. Monitor progress via SSE /api/v1/events.",
        }

    async def cancel(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a running plan application.

        Sets the circuit breaker to signal cancellation.
        """
        tracker = get_task_tracker()
        key = f"{workspace_path}:{gateway or 'default'}"

        cancelled = tracker.cancel(key)

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
    # Internal helpers
    # -------------------------------------------------------------------------

    def _collect_lint_violations(self, context) -> Dict[str, Any]:
        """Run ``context.lint_models(raise_on_error=False)`` and return UI-friendly violations.

        SQLMesh exposes linting via ``Context.lint_models`` (there is no
        ``Context.lint``). Passing ``raise_on_error=False`` returns the full
        list of ``AnnotatedRuleViolation`` instead of raising ``LinterError``,
        so we can surface every per-file/per-rule finding.

        Lint runs synchronously on a worker thread. The method is tolerant of
        partial failures: if the linter is unavailable or invocation fails, the
        returned payload still has a single fallback violation so the user sees
        SOMETHING actionable.
        """
        if _LinterErrorCls is None:
            return {
                "total": 0,
                "errors": 0,
                "warnings": 0,
                "by_file": [],
                "report": "",
                "message": "Linter information unavailable (SQLMesh version too old).",
            }
        try:
            # ``lint_models`` is the canonical entry point; guard against older
            # SQLMesh builds that may not expose it.
            lint_fn = getattr(context, "lint_models", None)
            if lint_fn is None:
                raise AttributeError("Context has no 'lint_models' method")
            violations = lint_fn(raise_on_error=False) or []
            return format_lint_violations(violations)
        except Exception as e:
            logger.warning("plan_lint_collect_failed", error=str(e))
            return {
                "total": 1,
                "errors": 1,
                "warnings": 0,
                "by_file": [
                    {
                        "model_name": "",
                        "file_path": "",
                        "rule": "",
                        "message": str(e),
                        "severity": "error",
                        "line": None,
                        "column": None,
                    }
                ],
                "report": str(e),
                "message": str(e),
            }

    def _run_tests_summary(self, context) -> Optional[Dict[str, Any]]:
        """Run all unit tests synchronously and return a structured summary.

        Returns None when tests cannot be run; otherwise a dict matching
        PlanTestResults (total/failures/errors/successful/dialect/details/traceback).
        Runs inside a worker thread via asyncio.to_thread.
        """
        try:
            result = context.test()
        except Exception as e:
            # Test infrastructure failed to run at all; treat as no tests so the
            # plan build can surface the real error downstream.
            logger.warning("plan_tests_run_failed", error=str(e))
            return None

        # Resolve the target dialect for display (best-effort).
        dialect = ""
        try:
            dialect = context.test_connection_config._engine_adapter.DIALECT
        except Exception:
            dialect = getattr(context, "default_dialect", "") or ""

        return format_test_failure_summary(result, dialect)

    def _build_plan(
        self,
        context,
        environment: str = "dev",
        plan_options: Optional[dict] = None,
        plan_dates: Optional[dict] = None,
        categories: Optional[dict] = None,
    ):
        """Build a PlanBuilder synchronously (runs in thread)."""
        plan_options = plan_options or {}
        plan_dates = plan_dates or {}

        kwargs = {
            "environment": environment,
            "no_prompts": True,
            "skip_tests": plan_options.get("skip_tests", False),
            "skip_backfill": plan_options.get("skip_backfill", False),
            "forward_only": plan_options.get("forward_only", False),
            "include_unmodified": plan_options.get("include_unmodified", False),
            "no_gaps": plan_options.get("no_gaps", False),
            "no_auto_categorization": plan_options.get("no_auto_categorization", False),
        }

        if plan_options.get("create_from"):
            kwargs["create_from"] = plan_options["create_from"]
        if plan_options.get("restate_models"):
            kwargs["restate_models"] = plan_options["restate_models"]
        if plan_dates.get("start"):
            kwargs["start"] = plan_dates["start"]
        if plan_dates.get("end"):
            kwargs["end"] = plan_dates["end"]

        # Keep backward/forward compatibility across SQLMesh versions:
        # if a kwarg is not supported by current plan_builder signature,
        # remove it and retry once per unsupported argument.
        while True:
            try:
                return context.plan_builder(**kwargs)
            except TypeError as exc:
                match = re.search(r"unexpected keyword argument '([^']+)'", str(exc))
                if not match:
                    raise

                unsupported_kwarg = match.group(1)
                if unsupported_kwarg not in kwargs:
                    raise

                kwargs.pop(unsupported_kwarg)
                logger.warning(
                    "plan_builder_unsupported_kwarg_removed",
                    unsupported_kwarg=unsupported_kwarg,
                    environment=environment,
                )

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
            from src.core.history_service import get_history_service
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
            logger.error("apply_history_save_failed", error=str(e), workspace_path=workspace_path)

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


# Singleton
_plan_service: Optional[PlanService] = None


def get_plan_service() -> PlanService:
    global _plan_service
    if _plan_service is None:
        _plan_service = PlanService()
    return _plan_service
