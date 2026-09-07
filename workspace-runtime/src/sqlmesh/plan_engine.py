"""Context-level plan orchestration — one implementation, two callers (P6c).

Everything here operates on an already-loaded SQLMesh ``Context`` and returns
pure data. It exists as a module so the WARM SANDBOX (executor server, where the
Context legitimately lives because user code executes there) and any future
caller run byte-for-byte the same logic. Before P6c this lived inside
``PlanService``; keeping it there would have forced a second copy into the
sandbox, and two copies of plan semantics drift exactly the way two copies of a
security check do.

The leaf helpers (kwargs mapping, test/lint formatting, failed-model capture)
live in ``datacode_common.sqlmesh_support`` because the one-shot task-runner
needs the same ones and cannot import this package. This module keeps only the
composition that is runtime-specific: building a plan and formatting it with the
platform's rich DTOs.

What deliberately stays OUT of this module: everything about HTTP, SSE, locks,
history, fingerprints or the context pool. Those are service concerns; the
engine is deliberately callable from a bare child process.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from datacode_common.sqlmesh_support import (
    FailedModelsConsole,
    apply_category_choices,
    apply_plan,
    build_failed_models_report,
    call_plan_builder,
    capture_failed_models,
    collect_lint_violations,
    format_lint_violations,
    format_test_failure_summary,
    is_linter_error,
    plan_builder_kwargs,
    run_tests_summary,
    tests_failed,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Historical name: plan_service and its tests imported the console proxy from
# here. Keep both names pointing at the ONE implementation.
FailedModelsCapturingConsole = FailedModelsConsole


def build_plan(
    context: Any,
    environment: str,
    *,
    plan_options: Optional[dict] = None,
    plan_dates: Optional[dict] = None,
    categories: Optional[dict] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Build (and optionally re-categorize) a plan, returning ``(plan, formatted)``.

    Category choices: when the user picked breaking/non-breaking per modified
    model, the plan is rebuilt with those choices applied, so the formatted
    result the UI sees is the plan that a subsequent apply would execute.
    """
    from src.sqlmesh.format import format_plan_result

    kwargs = plan_builder_kwargs(plan_options, plan_dates)
    kwargs["environment"] = environment
    builder = call_plan_builder(context, kwargs)
    plan = builder.build()
    plan = apply_category_choices(builder, plan, categories or {})
    return plan, format_plan_result(plan, context=context)


def plan_no_changes(plan: Any) -> bool:
    """The plan has nothing to apply — used to short-circuit the apply flow."""
    return not plan.has_changes and not plan.requires_backfill


__all__ = [
    "FailedModelsCapturingConsole",
    "apply_plan",
    "build_failed_models_report",
    "build_plan",
    "call_plan_builder",
    "capture_failed_models",
    "collect_lint_violations",
    "format_lint_violations",
    "format_test_failure_summary",
    "is_linter_error",
    "plan_builder_kwargs",
    "plan_no_changes",
    "run_tests_summary",
    "tests_failed",
]
