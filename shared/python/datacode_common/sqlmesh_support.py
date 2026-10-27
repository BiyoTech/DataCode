"""SQLMesh leaf helpers shared by every execution vehicle (P6c).

Three processes need the SAME logic over a SQLMesh Context — the warm sandbox
(executor server), the one-shot task-runner, and workspace-runtime itself. The
functions here are the ones where a second copy would be a correctness hazard,
not merely duplication:

* ``plan_builder_kwargs`` / ``call_plan_builder`` — the mapping from UI plan
  options to engine kwargs, including the version-compat retry. Two copies
  would mean two places to forget that ``create_from`` must never appear here.
* ``run_tests_summary`` / ``format_test_failure_summary`` — the test gate both
  the sandbox preview and the apply flow depend on; disagreeing copies would
  let an apply proceed on tests the preview would have blocked.
* ``format_lint_violations`` — structured lint output. The runtime renders a
  422 from it; the task-runner embeds it in a failure result. Same shape or the
  frontend sees two different panels for the same problem.
* ``FailedModelsConsole`` — the only way to recover per-model backfill failure
  detail from SQLMesh's detail-less PlanError.

Everything takes sqlmesh objects but imports sqlmesh only lazily inside the
functions, so importing this module stays cheap and works before the engine is
needed.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_LINTER_ERROR_CLS = None


def _linter_error_cls():
    """Resolve ``LinterError`` once, tolerating older SQLMesh builds."""
    global _LINTER_ERROR_CLS
    if _LINTER_ERROR_CLS is None:
        try:
            from sqlmesh.utils.errors import LinterError

            _LINTER_ERROR_CLS = LinterError
        except ImportError:  # pragma: no cover - older SQLMesh fallback
            _LINTER_ERROR_CLS = False
    return _LINTER_ERROR_CLS or None


def is_linter_error(exc: BaseException) -> bool:
    """Whether an exception raised by plan building is a lint rejection."""
    linter_cls = _linter_error_cls()
    return linter_cls is not None and isinstance(exc, linter_cls)


# ── plan builder kwargs ────────────────────────────────────────────────────


def plan_builder_kwargs(
    plan_options: Optional[dict], plan_dates: Optional[dict]
) -> Dict[str, Any]:
    """Translate UI plan options into ``Context.plan_builder`` kwargs.

    ``create_from`` is deliberately NOT accepted: creating an environment is a
    registry operation (branch + worktree + virtual environment + deployment),
    never a side effect of a plan. The registry's SQLMesh binder is the only
    caller allowed to use it — a constraint enforced by omission, in every copy
    of this function at once because there is only one copy.
    """
    plan_options = plan_options or {}
    plan_dates = plan_dates or {}

    kwargs: Dict[str, Any] = {
        "environment": None,  # filled by the caller
        "no_prompts": True,
        "skip_tests": bool(plan_options.get("skip_tests", False)),
        "skip_backfill": bool(plan_options.get("skip_backfill", False)),
        "forward_only": bool(plan_options.get("forward_only", False)),
        "include_unmodified": bool(plan_options.get("include_unmodified", False)),
        "no_gaps": bool(plan_options.get("no_gaps", False)),
        "no_auto_categorization": bool(
            plan_options.get("no_auto_categorization", False)
        ),
    }
    if plan_options.get("restate_models"):
        kwargs["restate_models"] = plan_options["restate_models"]
    if plan_dates.get("start"):
        kwargs["start"] = plan_dates["start"]
    if plan_dates.get("end"):
        kwargs["end"] = plan_dates["end"]
    return kwargs


def call_plan_builder(context: Any, kwargs: Dict[str, Any]):
    """Invoke ``context.plan_builder`` with version-compat kwarg pruning.

    SQLMesh's plan_builder signature moves between versions; an unsupported
    kwarg raises ``TypeError`` naming it. Dropping the named kwarg and retrying
    keeps the platform usable across engine upgrades — each removal is logged so
    a silently-lost option is visible, never silent.
    """
    kwargs = dict(kwargs)
    while True:
        try:
            return context.plan_builder(**kwargs)
        except TypeError as exc:
            match = re.search(r"unexpected keyword argument '([^']+)'", str(exc))
            if not match or match.group(1) not in kwargs:
                raise
            unsupported = match.group(1)
            kwargs.pop(unsupported)
            logger.warning(
                "plan_builder_unsupported_kwarg_removed",
                extra={"unsupported_kwarg": unsupported},
            )


def apply_category_choices(builder: Any, plan: Any, categories: dict) -> Any:
    """Set user-chosen change categories and rebuild, returning the new plan.

    A no-op (returns the original plan) when no choice matches a new snapshot —
    the common case, since categories are only offered for modified models.
    """
    categories = categories or {}
    if not categories or not getattr(plan.context_diff, "has_changes", False):
        return plan

    from sqlmesh.core.snapshot.definition import SnapshotChangeCategory

    applied = False
    for new, _ in plan.context_diff.modified_snapshots.values():
        if plan.is_new_snapshot(new) and new.name in categories:
            builder.set_choice(new, SnapshotChangeCategory[categories[new.name]])
            applied = True
    return builder.build() if applied else plan


# ── tests ──────────────────────────────────────────────────────────────────


def format_test_failure_summary(result: Any, dialect: str = "") -> dict:
    """Build a structured test failure summary for the plan UI.

    Mirrors the SQLMesh web server's ReportTestsFailure payload so the frontend
    can render a rich "One or More Tests Failed" panel with totals and
    per-test tracebacks.
    """
    from sqlmesh.core.test import ModelTest

    # Per-test failure/error details (message + formatted traceback).
    details = []
    for test, tb in list(result.failures) + list(result.errors):
        if isinstance(test, ModelTest):
            model_name = getattr(getattr(test, "model", None), "name", "")
            message = f"Failure test: {model_name} {test.test_name}".strip()
        else:
            message = f"Failure test: {getattr(test, 'test_name', str(test))}"
        details.append({"message": message, "details": tb})

    failures = len(result.failures)
    errors = len(result.errors)

    return {
        "total": result.testsRun,
        "failures": failures,
        "errors": errors,
        "successful": result.testsRun - failures - errors,
        "dialect": dialect,
        "details": details,
        # Combined traceback text (joined per-test details for plain display).
        "traceback": "\n\n".join(d["details"] for d in details),
    }


def run_tests_summary(context: Any) -> Optional[Dict[str, Any]]:
    """Run all unit tests and return a structured summary.

    Returns None when the test infrastructure cannot run at all, so a broken
    harness surfaces the REAL error from the plan build downstream instead of a
    misleading "tests failed" verdict.
    """
    try:
        result = context.test()
    except Exception as exc:  # noqa: BLE001 — harness failure is not test failure
        logger.warning("plan_tests_run_failed", extra={"error": str(exc)})
        return None

    dialect = ""
    try:
        dialect = context.test_connection_config._engine_adapter.DIALECT
    except Exception:  # noqa: BLE001 — display-only
        dialect = getattr(context, "default_dialect", "") or ""

    return format_test_failure_summary(result, dialect)


def tests_failed(summary: Optional[Dict[str, Any]]) -> bool:
    """Whether a test summary reports failures that must abort an apply."""
    return bool(
        summary
        and summary.get("total", 0) > 0
        and (summary.get("failures") or summary.get("errors"))
    )


# ── lint ───────────────────────────────────────────────────────────────────


def format_lint_violations(violations: Iterable) -> Dict[str, Any]:
    """Convert SQLMesh ``AnnotatedRuleViolation`` objects to a UI-friendly dict.

    The shape mirrors the SQLMesh CLI's per-file output so the frontend can show
    a familiar "rule: message" line plus the offending file. Tolerant of missing
    attributes (older SQLMesh versions, partial violations) so a single bad
    record never breaks the whole plan.
    """
    by_file: List[Dict[str, Any]] = []
    errors = 0
    warnings = 0
    seen_keys: set = set()

    for v in violations:
        try:
            rule_name = getattr(getattr(v, "rule", None), "name", "") or ""
            message = getattr(v, "violation_msg", "") or ""
            model = getattr(v, "model", None)
            model_name = (
                getattr(model, "name", "") or getattr(model, "_path", "") or ""
            )
            file_path = ""
            if model is not None:
                p = getattr(model, "_path", None)
                if p is not None:
                    try:
                        file_path = str(p)
                    except Exception:  # noqa: BLE001 — display-only
                        file_path = ""
            severity = getattr(v, "violation_type", "error") or "error"
            line = None
            column = None
            rng = getattr(v, "violation_range", None)
            start = getattr(rng, "start", None) if rng is not None else None
            if start is not None:
                # ``start`` is a ``Range.Position`` with 0-based line/column.
                line = getattr(start, "line", None)
                if line is not None:
                    line = int(line) + 1  # 1-based for display
                col = getattr(start, "column", None)
                if col is not None:
                    column = int(col) + 1
        except Exception:  # noqa: BLE001 — one unreadable violation
            rule_name, message, model_name, file_path, severity = (
                "",
                str(v),
                "",
                "",
                "error",
            )
            line = column = None

        if severity == "error":
            errors += 1
        elif severity == "warning":
            warnings += 1

        # Deduplicate identical entries (a rule can fire twice on a model).
        key = (file_path, model_name, rule_name, message, severity, line, column)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        by_file.append(
            {
                "model_name": model_name,
                "file_path": file_path,
                "rule": rule_name,
                "message": message,
                "severity": severity,
                "line": line,
                "column": column,
            }
        )

    return {
        "total": len(by_file),
        "errors": errors,
        "warnings": warnings,
        "by_file": by_file,
        "report": _build_lint_report(by_file),
        "message": (
            f"{errors} lint error(s), {warnings} warning(s) across "
            f"{len({v['file_path'] for v in by_file if v['file_path']})} file(s)"
            if by_file
            else ""
        ),
    }


def _build_lint_report(by_file: List[Dict[str, Any]]) -> str:
    """Render lint violations as native-CLI-style grouped text."""
    from collections import OrderedDict

    grouped: "OrderedDict[tuple, List[Dict[str, Any]]]" = OrderedDict()
    for v in by_file:
        key = (v.get("file_path") or "", v.get("severity") or "error")
        grouped.setdefault(key, []).append(v)

    blocks: List[str] = []
    for (file_path, severity), items in grouped.items():
        label = "errors" if severity == "error" else "warnings"
        header_target = file_path or (items[0].get("model_name") or "unknown")
        lines = [f"Linter {label} for {header_target}:"]
        for v in items:
            rule = v.get("rule") or ""
            message = v.get("message") or ""
            line_no = v.get("line")
            if line_no is not None:
                lines.append(f" - Line {line_no}: {rule} - {message}")
            else:
                lines.append(f" - {rule}: {message}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def collect_lint_violations(context: Any) -> Dict[str, Any]:
    """Run ``context.lint_models(raise_on_error=False)`` for UI-friendly output.

    Tolerant of partial failures: if the linter is unavailable or invocation
    fails, the payload still carries one fallback violation so the user sees
    SOMETHING actionable rather than an empty panel.
    """
    if _linter_error_cls() is None:
        return {
            "total": 0,
            "errors": 0,
            "warnings": 0,
            "by_file": [],
            "report": "",
            "message": "Linter information unavailable (SQLMesh version too old).",
        }
    try:
        lint_fn = getattr(context, "lint_models", None)
        if lint_fn is None:
            raise AttributeError("Context has no 'lint_models' method")
        violations = lint_fn(raise_on_error=False) or []
        return format_lint_violations(violations)
    except Exception as exc:  # noqa: BLE001 — lint must not 500 the plan
        logger.warning("plan_lint_collect_failed", extra={"error": str(exc)})
        return {
            "total": 1,
            "errors": 1,
            "warnings": 0,
            "by_file": [
                {
                    "model_name": "",
                    "file_path": "",
                    "rule": "",
                    "message": str(exc),
                    "severity": "error",
                    "line": None,
                    "column": None,
                }
            ],
            "report": str(exc),
            "message": str(exc),
        }


# ── apply-time failure capture ────────────────────────────────────────────


class FailedModelsConsole:
    """Transparent console proxy recording per-model backfill failures.

    During ``context.apply`` SQLMesh reports the real per-model failure ONLY via
    ``console.log_failed_models(errors)`` and then raises a detail-less
    ``PlanError``. Swapping the console for this proxy captures the actionable
    cause (matching the native CLI's "Failed models" report) while delegating
    everything else, so console behavior for other callers is unchanged.
    """

    def __init__(self, wrapped: Any, sink: List[Any]) -> None:
        # Bypass our own __setattr__ (which forwards to the wrapped console).
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_sink", sink)

    def log_failed_models(self, errors: Any) -> Any:
        try:
            self._sink.extend(list(errors))
        except Exception:  # noqa: BLE001 — capture must never break the apply
            pass
        return object.__getattribute__(self, "_wrapped").log_failed_models(errors)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_wrapped"), name, value)


def build_failed_models_report(errors: List[Any]) -> str:
    """Render captured node errors as the native-CLI-style report."""
    try:
        from sqlmesh.core.console import _format_node_errors

        formatted = _format_node_errors(errors)
        lines = ["Failed models", ""]
        for node_name, msg in formatted.items():
            lines.append(f"  {node_name}\n\n{msg.rstrip()}")
        return "\n".join(lines)
    except Exception as fmt_err:  # pragma: no cover — defensive formatting guard
        logger.warning(
            "failed_models_report_format_failed", extra={"error": str(fmt_err)}
        )
        return "Failed models\n\n" + "\n".join(
            f"  {str(getattr(e, '__cause__', None) or e)}" for e in errors
        )


@contextmanager
def capture_failed_models(context: Any):
    """Swap a context's console for the capturing proxy for the enclosed block.

    Yields the sink; whatever the plan raises is the caller's business — this
    only guarantees the console is restored and the sink is readable afterwards.
    """
    sink: List[Any] = []
    original = context.console
    context.console = FailedModelsConsole(original, sink)
    try:
        yield sink
    finally:
        context.console = original


def apply_plan(context: Any, plan: Any, should_cancel=None) -> List[Any]:
    """Apply a built plan, returning the captured per-model failures (if any).

    ``should_cancel`` mirrors the circuit-breaker callable SQLMesh's
    ``context.apply`` accepts. Cross-process callers pass nothing — their
    cancellation is the vehicle's kill (task timeout), which is the honest
    equivalent available through a socket.
    """
    with capture_failed_models(context) as sink:
        if should_cancel is None:
            context.apply(plan)
        else:
            context.apply(plan, should_cancel)
    return list(sink)


__all__ = [
    "FailedModelsConsole",
    "apply_category_choices",
    "apply_plan",
    "build_failed_models_report",
    "call_plan_builder",
    "capture_failed_models",
    "collect_lint_violations",
    "format_lint_violations",
    "format_test_failure_summary",
    "is_linter_error",
    "plan_builder_kwargs",
    "run_tests_summary",
    "tests_failed",
]
