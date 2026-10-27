"""SQLMesh operations — the work itself, with no transport knowledge.

Kept free of any I/O contract on purpose. Two vehicles execute these same
functions:

* **task-runner one-shot** (P6a, this package's ``main.py``): a container per
  task, spec in / result out.
* **warm sandbox** (P6c): a long-lived container speaking the frozen
  ``executor_protocol`` frames, replacing today's L3-proc child process.

If the operations lived inside either entry point, P6c would either duplicate
them or reshape the protocol. Progress reaches the caller through an injected
callback for the same reason.

Result shapes mirror ``executor_protocol``'s DTOs, so the runtime sees one shape
regardless of which vehicle produced it.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from datacode_common.workspace_layout import (
    RENDERED_PROJECT_DIR as _RENDERED_PROJECT_DIR,
)

# SQLMesh project sub-directory of a working copy. Mirrors
# ``workspace-runtime/src/files/layout.py``; duplicated as a constant rather
# than imported because the runner must not depend on the runtime package.
SQL_WAREHOUSE_DIR = "sql-warehouse"

# Directories whose ``.py`` files are executed when a Context loads.
_PYTHON_CODE_DIRS = ("models", "macros")

# Rows returned by an ad-hoc query. A sandbox must not be able to stream an
# unbounded result set back through the log channel.
DEFAULT_ROW_LIMIT = 1000
MAX_ROW_LIMIT = 10_000

ProgressFn = Callable[[str, Optional[int], str], None]


class SqlMeshOpError(RuntimeError):
    """An operation failed. Carries a stable protocol error code.

    Attributes:
        code: One of ``datacode_common.task_protocol``'s ``ERR_*`` values.
        detail: Structured facts the caller could not derive from the message —
            a dependency conflict's rewrite, for one. Travels in the failure's
            ``result`` payload, so a UI can act on it instead of parsing prose.
    """

    def __init__(
        self,
        code: str,
        message: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail: dict[str, Any] = dict(detail or {})


def _noop_progress(_stage: str, _percent: Optional[int], _detail: str) -> None:
    """Progress sink for callers that do not want progress."""


def resolve_sqlmesh_root(workspace_path: str, project_subdir: str = "") -> str:
    """Return the SQLMesh project directory for a working copy.

    A working copy holds two sibling projects (``sql-warehouse/`` and
    ``asset-dag/``), so the SQLMesh root is normally the former. A path that is
    itself a SQLMesh project is also accepted — the warm sandbox may mount the
    project directly — but only on the evidence of a ``config.yaml``, never as a
    silent fallback: guessing here would make SQLMesh load the wrong tree and
    report "no models" instead of a configuration error.

    Args:
        workspace_path: Working copy as this process sees it.
        project_subdir: Alternative project directory RELATIVE to the working
            copy. Used by the write split (design 04 §3.5): phase 2 acts on a
            rendered, SQL-only copy of the project rather than on the original.
            Traversal is refused — a task must not be able to point the engine at
            a directory outside its own mount.

    Raises:
        SqlMeshOpError: Neither layout is present, or the sub-directory escapes.
    """
    root = Path(workspace_path)

    if project_subdir:
        candidate = (root / project_subdir).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise SqlMeshOpError(
                "bad_spec",
                f"project_subdir {project_subdir!r} points outside the working copy",
            ) from None
        if not candidate.is_dir():
            raise SqlMeshOpError(
                "bad_spec", f"no project directory at {project_subdir!r}"
            )
        return str(candidate)

    warehouse = root / SQL_WAREHOUSE_DIR
    if warehouse.is_dir():
        return str(warehouse)
    if (root / "config.yaml").is_file() or (root / "config.yml").is_file():
        return str(root)
    raise SqlMeshOpError(
        "bad_spec",
        f"no SQLMesh project at {workspace_path}: expected a "
        f"{SQL_WAREHOUSE_DIR}/ sub-directory or a config.yaml",
    )


def find_python_code(sqlmesh_root: str) -> list[str]:
    """List the project files that make ``Context()`` execute user code.

    Used to enforce the L2 precondition (see :func:`assert_sql_only`), so it
    reports paths rather than a boolean: an operator refused an L2 run needs to
    know which file caused it.
    """
    root = Path(sqlmesh_root)
    found: list[str] = []
    for directory in _PYTHON_CODE_DIRS:
        candidate = root / directory
        if not candidate.is_dir():
            continue
        for path in sorted(candidate.rglob("*.py")):
            if path.name == "__init__.py" and path.stat().st_size == 0:
                # An empty package marker executes nothing.
                continue
            found.append(str(path.relative_to(root)))
    return found


def assert_sql_only(sqlmesh_root: str) -> None:
    """Refuse to continue if the project can execute host-language code.

    This is the L2 precondition, and it is enforced here rather than trusted
    from the submitter because L2 is the ONE profile holding a WRITABLE
    warehouse account (Q5, design 04 §3.5). Running a user's Python model under
    that account is precisely the combination the split exists to prevent, so an
    L2 task facing Python code must fail rather than proceed — the runtime is
    expected to route such work to ``sqlmesh.plan_with_python`` (L3-base,
    read-only) instead.

    Raises:
        SqlMeshOpError: The project contains Python models or macros.
    """
    offenders = find_python_code(sqlmesh_root)
    if offenders:
        raise SqlMeshOpError(
            "bad_spec",
            "this task kind runs in an SQL-only profile, but the project "
            f"contains host-language code: {', '.join(offenders[:5])}"
            + (f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else "")
            + " — route it to a plan_with_python task kind instead",
        )


def load_context(sqlmesh_root: str, gateway: str = ""):
    """Build the executable SQLMesh Context.

    THE call this whole sandbox exists for: ``Context()`` imports and executes
    the project's Python models and macros. It happens here, where there are no
    platform credentials to read, rather than in the runtime process.
    """
    # SQLMesh phones home by default; a sandbox with a default-deny egress
    # policy would spend its startup budget on a blocked connection.
    os.environ["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] = "true"

    from sqlmesh.core.context import Context

    try:
        if gateway:
            return Context(paths=sqlmesh_root, gateway=gateway)
        return Context(paths=sqlmesh_root)
    except Exception as exc:  # noqa: BLE001 — user project may fail any way
        raise SqlMeshOpError(
            "task_failed", f"could not load the SQLMesh project: {exc}"
        ) from exc


def _snapshot_names(snapshots: Any) -> list[str]:
    """Reduce a snapshot collection to plain names.

    Snapshots carry live objects (SQLGlot ASTs, engine adapters); only names may
    cross the boundary back to the runtime.
    """
    return [str(getattr(item, "name", item)) for item in (snapshots or [])]


def _plan_to_dict(plan: Any, environment: str) -> dict[str, Any]:
    """Reduce a SQLMesh plan to the frozen ``PlanResultDTO`` shape."""
    return {
        "environment": environment,
        "direct": _snapshot_names(getattr(plan, "direct", None)),
        "indirect": _snapshot_names(getattr(plan, "indirect", None)),
        "metadata_changed": _snapshot_names(getattr(plan, "metadata_updated", None)),
        "breaking": _snapshot_names(getattr(plan, "breaking", None)),
        "forward_only": bool(getattr(plan, "forward_only", False)),
        "has_changes": bool(getattr(plan, "has_changes", False)),
        "requires_backfill": bool(getattr(plan, "requires_backfill", False)),
        "plan_id": str(getattr(plan, "plan_id", "") or ""),
        "diff_summary": "",
    }


def _build_and_categorize(
    context: Any,
    environment: str,
    *,
    plan_options: Mapping[str, Any],
    plan_dates: Mapping[str, Any],
    categories: Mapping[str, Any],
):
    """Build a plan with the caller's options, then apply category choices.

    Shared by op_plan and op_apply so preview and write categorize IDENTICALLY;
    a divergence would let a preview show a non-breaking change that the apply
    then records as breaking. The kwargs mapping and the version-compat retry
    come from the shared library — one implementation, used by the warm sandbox
    and the runtime too.
    """
    from datacode_common.sqlmesh_support import (
        apply_category_choices,
        call_plan_builder,
        plan_builder_kwargs,
    )

    options = dict(plan_options)
    # Tests are the caller's gate, run before submission; building here with
    # tests enabled would run them a second time for no decision value.
    options.setdefault("skip_tests", True)

    kwargs = plan_builder_kwargs(options, plan_dates)
    kwargs["environment"] = environment
    builder = call_plan_builder(context, kwargs)
    plan = builder.build()
    plan = apply_category_choices(builder, plan, categories)
    return plan


def _lint_failure(context: Any, exc: BaseException) -> SqlMeshOpError:
    """Translate a linter rejection into a structured, self-describing error.

    The lint REPORT travels in the message (the task result channel carries
    strings); the runtime turns the ``lint_failed`` code into its structured 422
    and the report is the actionable text either way.
    """
    from datacode_common.sqlmesh_support import collect_lint_violations

    lint = collect_lint_violations(context)
    report = (lint.get("report") or lint.get("message") or "").strip()
    raise SqlMeshOpError(
        "lint_failed",
        report or f"plan blocked by linter: {exc}",
    ) from exc


def op_plan(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = True,
    project_subdir: str = "",
    progress: ProgressFn = _noop_progress,
) -> dict[str, Any]:
    """Compute a plan WITHOUT applying it.

    Params (all optional): ``plan_options``, ``plan_dates``, ``categories`` —
    the same shapes the REST plan endpoints accept, so a caller routing a plan
    through a task gets identical semantics to the warm-sandbox preview.

    Args:
        workspace_path: Working copy as seen by this process.
        gateway: Warehouse gateway, empty for the project default.
        environment: Target environment. Empty resolves to the project default
            rather than to ``prod``: defaulting a *plan* to production is how an
            unrelated environment gets diffed against the wrong target.
        sql_only: Enforce the L2 precondition first.
        project_subdir: Act on this project instead of the working copy's own —
            used by phase 2 of the write split.
        progress: Progress sink.
    """
    sqlmesh_root = resolve_sqlmesh_root(workspace_path, project_subdir)
    if sql_only:
        assert_sql_only(sqlmesh_root)

    progress("loading_project", 10, sqlmesh_root)
    context = load_context(sqlmesh_root, gateway)
    target = environment or str(getattr(context, "default_environment", "") or "prod")

    progress("planning", 50, target)
    try:
        plan = _build_and_categorize(
            context,
            target,
            plan_options=params.get("plan_options") or {},
            plan_dates=params.get("plan_dates") or {},
            categories=params.get("categories") or {},
        )
    except SqlMeshOpError:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaces the engine's own error
        from datacode_common.sqlmesh_support import is_linter_error

        if is_linter_error(exc):
            raise _lint_failure(context, exc)
        raise SqlMeshOpError("task_failed", f"plan failed: {exc}") from exc

    progress("done", 100, "")
    return _plan_to_dict(plan, target)


def op_apply(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = True,
    project_subdir: str = "",
    progress: ProgressFn = _noop_progress,
) -> dict[str, Any]:
    """Compute a plan and apply it — the Q5 write half.

    ``sql_only`` defaults to True because this is the only operation that needs
    the writable warehouse account; when the project contains Python, the caller
    routes through the write split (render in L3, apply the rendering here).

    Params: ``plan_options`` / ``plan_dates`` / ``categories`` — the same shapes
    the REST apply endpoint accepts. Tests are NOT run here: the caller's gate
    (warm-sandbox preview) already ran them, and re-running them in the write
    vehicle would double the cost of every apply.

    ``project_subdir`` is how phase 2 of the write split acts on the RENDERED
    project: that project contains no Python, so ``assert_sql_only`` passes on
    its own merits rather than being skipped for this case.
    """
    sqlmesh_root = resolve_sqlmesh_root(workspace_path, project_subdir)
    if sql_only:
        assert_sql_only(sqlmesh_root)

    progress("loading_project", 10, sqlmesh_root)
    context = load_context(sqlmesh_root, gateway)
    target = environment or str(getattr(context, "default_environment", "") or "prod")

    progress("applying", 50, target)
    started = time.monotonic()
    try:
        plan = _build_and_categorize(
            context,
            target,
            plan_options=params.get("plan_options") or {},
            plan_dates=params.get("plan_dates") or {},
            categories=params.get("categories") or {},
        )
    except SqlMeshOpError:
        raise
    except Exception as exc:  # noqa: BLE001
        from datacode_common.sqlmesh_support import is_linter_error

        if is_linter_error(exc):
            raise _lint_failure(context, exc)
        raise SqlMeshOpError("task_failed", f"apply failed: {exc}") from exc

    if not plan.has_changes and not plan.requires_backfill:
        # Nothing to write. Reported rather than applied so the caller can
        # distinguish "wrote nothing" from "wrote" in its audit trail.
        result = _plan_to_dict(plan, target)
        result["applied"] = False
        result["reason"] = "no_changes"
        progress("done", 100, "")
        return result

    from datacode_common.sqlmesh_support import apply_plan, build_failed_models_report

    failed_models = apply_plan(context, plan)
    if failed_models:
        # The per-model causes are only reachable through the console capture;
        # include them so the failure names the model, not just "plan failed".
        report = build_failed_models_report(failed_models)
        raise SqlMeshOpError(
            "task_failed",
            f"{report}\n\nPlan application failed.",
        )

    result = _plan_to_dict(plan, target)
    result["applied"] = True
    result["apply_duration_ms"] = int((time.monotonic() - started) * 1000)
    progress("done", 100, "")
    return result


def op_query(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = True,
    project_subdir: str = "",
    progress: ProgressFn = _noop_progress,
) -> dict[str, Any]:
    """Run an ad-hoc read query against the warehouse.

    The row limit is clamped here, not just validated: an unbounded result set
    would travel back through the log channel and could exhaust the caller's
    memory rather than this sandbox's.
    """
    del environment
    sql = str(params.get("sql") or "").strip()
    if not sql:
        raise SqlMeshOpError("bad_spec", "query requires a non-empty 'sql' parameter")

    requested = int(params.get("row_limit") or DEFAULT_ROW_LIMIT)
    row_limit = max(1, min(requested, MAX_ROW_LIMIT))

    sqlmesh_root = resolve_sqlmesh_root(workspace_path, project_subdir)
    if sql_only:
        assert_sql_only(sqlmesh_root)

    progress("loading_project", 10, sqlmesh_root)
    context = load_context(sqlmesh_root, gateway)

    progress("querying", 50, "")
    try:
        frame = context.fetchdf(sql)
    except Exception as exc:  # noqa: BLE001
        raise SqlMeshOpError("task_failed", f"query failed: {exc}") from exc

    truncated = len(frame) > row_limit
    limited = frame.head(row_limit)
    progress("done", 100, "")
    return {
        "columns": [str(c) for c in limited.columns],
        "rows": limited.astype(object).where(limited.notna(), None).values.tolist(),
        "row_count": int(len(limited)),
        "truncated": truncated,
        "row_limit": row_limit,
    }


# ── the write split (design 04 §3.5, Q5) ──────────────────────────────────
#
# An ``apply`` on a project containing Python models is the one case where the two
# constraints collide: it needs a WRITABLE warehouse account, and it needs to
# execute user code. Q5 forbids putting those together, so the work is split:
#
#   phase 1 — ``sqlmesh.render_project`` in L3-base with a READ-ONLY account.
#             The Python models execute here and are rendered to plain SQL.
#   phase 2 — ``sqlmesh.apply`` in L2 with the writable account, acting on the
#             RENDERED project, which by construction contains no Python.
#
# The property that makes this sound: phase 2 does not need its check bypassed.
# The rendered project genuinely IS SQL-only, so ``assert_sql_only`` passes on its
# own merits. A design where L2 had to skip the check for this case would have
# moved the risk rather than removed it.

# Directory the rendered project is written to, inside the working copy. Under
# the platform's own state directory (``.datacode/``), which the workspace
# ``.gitignore`` covers and neither the explorer nor the agent lists — a
# rendered copy of the user's project must never look like a change of theirs.
RENDERED_PROJECT_DIR = _RENDERED_PROJECT_DIR


def op_render_project(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = False,
    project_subdir: str = "",
    progress: ProgressFn = _noop_progress,
) -> dict[str, Any]:
    """Render every model to plain SQL, producing an SQL-only project copy.

    Phase 1 of the write split. Runs where user code is expected (L3-base,
    read-only account): loading the Context executes the project's Python models
    and macros, and each model's query is then rendered to SQL text.

    ``sql_only`` defaults to False here — unlike every other op — precisely
    because this operation exists to handle projects that contain Python.

    Returns:
        The rendered project's path (relative to the working copy, since the next
        phase runs in a different sandbox with a different mount point), plus what
        was rendered and what was copied verbatim.
    """
    del params
    source_root = Path(resolve_sqlmesh_root(workspace_path, project_subdir))
    target_root = Path(workspace_path) / RENDERED_PROJECT_DIR

    progress("loading_project", 10, str(source_root))
    context = load_context(str(source_root), gateway)

    progress("rendering", 40, "")
    models = getattr(context, "models", {}) or {}
    rendered: list[str] = []
    failures: list[str] = []

    _reset_directory(target_root)
    # Configuration, audits and seeds are copied verbatim: they contain no
    # executable code, and rewriting them would risk changing behaviour between
    # the two phases — which is the one thing this split must not do.
    copied = _copy_project_scaffolding(source_root, target_root)

    models_dir = target_root / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    for name, model in models.items():
        try:
            sql = _render_model_definition(context, model)
        except Exception as exc:  # noqa: BLE001 — a user model may fail any way
            failures.append(f"{name}: {exc}")
            continue
        (models_dir / f"{_safe_filename(str(name))}.sql").write_text(
            sql, encoding="utf-8"
        )
        rendered.append(str(name))

    if failures:
        # Partial output is worse than none: phase 2 would apply a project that
        # silently lost models, which the user would read as data disappearing.
        _reset_directory(target_root)
        raise SqlMeshOpError(
            "task_failed",
            f"could not render {len(failures)} of {len(models)} models, so the "
            f"write phase would act on an incomplete project: {'; '.join(failures[:3])}"
            + (f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""),
        )

    progress("done", 100, "")
    return {
        "rendered_project": RENDERED_PROJECT_DIR,
        "models_rendered": sorted(rendered),
        "model_count": len(rendered),
        "files_copied": sorted(copied),
        # Proves the phase-2 precondition holds, rather than asserting it later.
        "python_code_remaining": find_python_code(str(target_root)),
    }


def _render_model_definition(context: Any, model: Any) -> str:
    """Render one model to a self-contained SQL definition.

    The MODEL block is regenerated from the model's own definition rather than
    copied from the source file: a Python model has no SQL source to copy, and a
    SQL model's file may contain macros that are exactly what must not survive
    into phase 2.
    """
    query = context.render(model.name) if hasattr(context, "render") else None
    if query is None:
        query = model.render_query()
    if query is None:
        raise ValueError("model produced no query")

    dialect = str(
        getattr(model, "dialect", "") or getattr(context, "default_dialect", "")
    )
    sql = (
        query.sql(dialect=dialect, pretty=True) if hasattr(query, "sql") else str(query)
    )

    properties = [f"name {model.name}"]
    kind_name = getattr(getattr(model, "kind", None), "name", "") or "FULL"
    properties.append(f"kind {kind_name}")
    if dialect:
        properties.append(f"dialect {dialect}")

    body = ",\n".join(f"  {entry}" for entry in properties)
    return f"MODEL (\n{body}\n);\n\n{sql}\n"


def _copy_project_scaffolding(source: Path, target: Path) -> list[str]:
    """Copy the non-executable parts of a project.

    Everything EXCEPT ``models/`` and ``macros/``: those are the two directories
    whose ``.py`` files execute, and they are what the rendering replaces. Caches
    and logs are skipped as regenerable noise.
    """
    skip = {"models", "macros", ".cache", "logs", "__pycache__", RENDERED_PROJECT_DIR}
    copied: list[str] = []
    for entry in sorted(source.iterdir()):
        if entry.name in skip or entry.name.startswith("."):
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)
        copied.append(entry.name)
    return copied


def _reset_directory(path: Path) -> None:
    """Make ``path`` an empty directory, removing any previous render."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _safe_filename(name: str) -> str:
    """Turn a model name into a filename that cannot escape the models directory."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return safe.strip("._") or "model"


__all__ = [
    "DEFAULT_ROW_LIMIT",
    "MAX_ROW_LIMIT",
    "RENDERED_PROJECT_DIR",
    "SQL_WAREHOUSE_DIR",
    "ProgressFn",
    "SqlMeshOpError",
    "assert_sql_only",
    "find_python_code",
    "load_context",
    "op_apply",
    "op_plan",
    "op_query",
    "op_render_project",
    "resolve_sqlmesh_root",
]
