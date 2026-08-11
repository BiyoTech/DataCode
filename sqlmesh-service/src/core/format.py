"""Unified serialization helpers for SQLMesh SDK objects.

All response formatting goes through this module to ensure consistent
JSON-serializable output. References the SQLMesh web server's serialization
approach for richer output.
"""

from __future__ import annotations

import typing as t
from collections import OrderedDict, defaultdict

from src.api.models.responses import (
    BackfillDetails,
    ChangeDirect,
    ChangeDisplay,
    ChangeIndirect,
    Column,
    ColumnLineageResponse,
    EnvironmentsResponse,
    EnvironmentSummary,
    LineageColumn,
    ModelDetails,
    ModelListItem,
    ModelLineageResponse,
    ModelResponse,
    ModelType,
    ModelsDiff,
    NodeType,
    PlanChanges,
    PlanResult,
    Reference,
    SchemaDiff,
    TableDiffResult,
    RowDiffSummary,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Model serialization
# ---------------------------------------------------------------------------

def _get_model_type(model) -> ModelType:
    """Determine model type enum value."""
    if model.is_sql:
        return ModelType.SQL
    if model.is_python:
        return ModelType.PYTHON
    return ModelType.SOURCE


def format_model_list_item(model) -> dict:
    """Lightweight model info for list endpoint (matches ModelListItem)."""
    path = model._path
    return ModelListItem(
        name=model.name,
        fqn=model.fqn,
        path=str(path.absolute().as_posix()) if path else None,
        dialect=model.dialect or "",
        type=_get_model_type(model),
        kind=model.kind.name if model.kind else None,
        description=model.description or None,
        owner=model.owner or None,
        tags=list(model.tags) if model.tags else [],
        cron=model.cron if hasattr(model, "cron") else None,
        gateway=getattr(model, "gateway", None),
    ).model_dump(exclude_none=True)


def format_model_detail(model, context=None, render_query: bool = True) -> dict:
    """Full model detail for get_model endpoint (matches ModelResponse).

    Args:
        model: SQLMesh Model object.
        context: SQLMesh Context (needed for column descriptions & path resolution).
        render_query: Whether to render the model's SQL query.
    """
    model_type = _get_model_type(model)
    dialect = model.dialect or ""
    default_catalog = getattr(model, "default_catalog", None) or (
        context.default_catalog if context else None
    )

    # Time column
    time_column = None
    if model.time_column:
        time_column = f"{model.time_column.column} | {model.time_column.format}"

    # Tags
    tags = ", ".join(model.tags) if model.tags else None

    # Partitioned by
    partitioned_by = None
    if model.partitioned_by:
        from sqlglot import exp as sqlglot_exp
        partitioned_by = ", ".join(
            expr.sql(pretty=True, dialect=model.dialect) for expr in model.partitioned_by
        )

    # Clustered by
    clustered_by = None
    if model.clustered_by:
        clustered_by = ", ".join(
            c.sql(dialect=model.dialect) for c in model.clustered_by
        )

    lookback = model.lookback if model.lookback > 0 else None

    # Columns with descriptions
    columns_to_types = model.columns_to_types or {}
    columns: t.List[Column] = []
    for col_name, data_type in columns_to_types.items():
        description = model.column_descriptions.get(col_name)
        if not description and render_query and context:
            try:
                from sqlmesh.core.lineage import column_description
                description = column_description(context, model.name, col_name, quote_column=True)
            except Exception:
                pass
        columns.append(Column(name=col_name, type=str(data_type), description=description))

    # References
    references = [
        Reference(name=ref.name, expression=ref.expression.sql(), unique=ref.unique)
        for ref in model.all_references
    ] if hasattr(model, "all_references") else []

    # Cron prev/next
    cron_prev = None
    cron_next = None
    try:
        from sqlmesh.utils.date import now, to_datetime
        cron_prev = str(to_datetime(model.cron_prev(value=now())))
        cron_next = str(to_datetime(model.cron_next(value=now())))
    except Exception:
        pass

    details = ModelDetails(
        owner=model.owner,
        kind=model.kind.name if model.kind else None,
        batch_size=model.batch_size,
        cron=model.cron,
        stamp=str(model.stamp) if model.stamp else None,
        start=str(model.start) if model.start else None,
        retention=model.retention,
        table_format=model.table_format,
        storage_format=model.storage_format,
        time_column=time_column,
        tags=tags,
        references=references,
        partitioned_by=partitioned_by,
        clustered_by=clustered_by,
        lookback=lookback,
        cron_prev=cron_prev,
        cron_next=cron_next,
        interval_unit=str(model.interval_unit) if model.interval_unit else None,
        annotated=model.annotated if hasattr(model, "annotated") else None,
    )

    # Render SQL
    sql = None
    if render_query:
        try:
            from sqlglot import exp as sqlglot_exp
            query = model.render_query() or (
                model.query if hasattr(model, "query") else sqlglot_exp.select('"FAILED TO RENDER QUERY"')
            )
            sql = query.sql(pretty=True, dialect=model.dialect)
        except Exception:
            sql = None

    path = model._path
    return ModelResponse(
        name=model.name,
        fqn=model.fqn,
        path=str(path.absolute().relative_to(context.path).as_posix()) if path and context else None,
        full_path=str(path.absolute().as_posix()) if path else None,
        dialect=dialect,
        type=model_type,
        columns=columns,
        description=model.description,
        details=details,
        sql=sql,
        default_catalog=default_catalog,
        hash=model.data_hash,
        gateway=getattr(model, "gateway", None),
    ).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Plan serialization
# ---------------------------------------------------------------------------

def format_plan_result(plan, context=None) -> dict:
    """Serialize a Plan object to rich API response (matches PlanResult).

    Includes added/removed/modified changes with display names,
    backfill details, and environment info.
    """
    context_diff = plan.context_diff

    # Environment name
    env_name = str(getattr(plan, "environment", "dev"))
    if hasattr(plan, "environment_naming_info") and plan.environment_naming_info:
        env_name = str(plan.environment_naming_info.name)

    # Default catalog from context
    default_catalog = context.default_catalog if context else None
    snapshots = context_diff.snapshots if context_diff else {}
    environment_naming_info = getattr(plan, "environment_naming_info", None)

    # Added
    added = []
    if context_diff and context_diff.added:
        for sid in context_diff.added:
            view_name = sid.name
            node_type = NodeType.MODEL
            if sid in snapshots:
                try:
                    view_name = snapshots[sid].display_name(environment_naming_info, default_catalog)
                    node_type = NodeType(snapshots[sid].node_type.value) if hasattr(snapshots[sid].node_type, "value") else NodeType.MODEL
                except Exception:
                    pass
            added.append(ChangeDisplay(name=sid.name, view_name=view_name, node_type=node_type))

    # Removed
    removed = []
    if context_diff and context_diff.removed_snapshots:
        for sid, snapshot_table_info in context_diff.removed_snapshots.items():
            view_name = sid.name
            node_type = NodeType.MODEL
            try:
                view_name = snapshot_table_info.display_name(environment_naming_info, default_catalog)
                node_type = NodeType(snapshot_table_info.node_type.value) if hasattr(snapshot_table_info.node_type, "value") else NodeType.MODEL
            except Exception:
                pass
            removed.append(ChangeDisplay(name=sid.name, view_name=view_name, node_type=node_type))

    # Modified (direct / indirect / metadata)
    modified = None
    if context_diff and context_diff.modified_snapshots:
        modified = _build_models_diff(context, plan, snapshots, environment_naming_info, default_catalog)

    changes = PlanChanges(added=added, removed=removed, modified=modified)

    # Backfills
    backfills = None
    if plan.requires_backfill:
        backfills = _build_backfill_details(plan, context, snapshots, environment_naming_info, default_catalog)

    return PlanResult(
        environment=env_name,
        has_changes=plan.has_changes,
        requires_backfill=plan.requires_backfill,
        changes=changes,
        backfills=backfills,
        start=str(plan.start) if plan.start else None,
        end=str(plan.end) if plan.end else None,
    ).model_dump(exclude_none=True)


def _build_models_diff(context, plan, snapshots, environment_naming_info, default_catalog) -> ModelsDiff:
    """Build modified snapshots breakdown (direct/indirect/metadata)."""
    context_diff = plan.context_diff
    direct: t.List[ChangeDirect] = []
    indirect: t.List[ChangeIndirect] = []
    metadata: t.List[ChangeDisplay] = []

    for name, (current, _) in context_diff.modified_snapshots.items():
        view_name = name
        node_type = NodeType.MODEL
        try:
            view_name = current.display_name(environment_naming_info, default_catalog)
            node_type = NodeType(current.node_type.value) if hasattr(current.node_type, "value") else NodeType.MODEL
        except Exception:
            pass

        if context_diff.directly_modified(name):
            diff = context_diff.text_diff(name) or ""
            change_category = None
            if hasattr(current, "change_category") and current.change_category:
                change_category = str(current.change_category.name)
            direct.append(ChangeDirect(
                name=name,
                view_name=view_name,
                node_type=node_type,
                diff=diff,
                change_category=change_category,
            ))
        elif context_diff.indirectly_modified(name):
            indirect.append(ChangeIndirect(
                name=name,
                view_name=view_name,
                node_type=node_type,
            ))
        elif context_diff.metadata_updated(name):
            metadata.append(ChangeDisplay(
                name=name,
                view_name=view_name,
                node_type=node_type,
            ))

    # Link parent relationships for direct changes
    for c in direct:
        c.indirect = [change for change in indirect if c.name in change.parents]
        c.direct = [change for change in direct if c.name in change.parents]

    return ModelsDiff(direct=direct, indirect=indirect, metadata=metadata)


def _build_backfill_details(plan, context, snapshots, environment_naming_info, default_catalog) -> t.List[BackfillDetails]:
    """Build backfill details list from plan missing intervals."""
    try:
        from sqlmesh.utils.date import make_inclusive, to_ds
        result = []
        for interval in plan.missing_intervals:
            view_name = interval.snapshot_id.name
            try:
                if interval.snapshot_id in snapshots:
                    view_name = snapshots[interval.snapshot_id].display_name(
                        environment_naming_info, default_catalog
                    )
            except Exception:
                pass

            interval_strs = []
            if interval.merged_intervals:
                for start, end in interval.merged_intervals:
                    interval_strs.append(
                        f"{to_ds(start)} -> {to_ds(end)}"
                    )

            result.append(BackfillDetails(
                name=interval.snapshot_id.name,
                view_name=view_name,
                interval=interval_strs[:1] if interval_strs else [],
                batches=len(interval.merged_intervals) if interval.merged_intervals else 0,
            ))
        return result
    except Exception as e:
        logger.warning("backfill_details_failed", error=str(e))
        return []


# ---------------------------------------------------------------------------
# Lineage serialization
# ---------------------------------------------------------------------------

def format_lineage(graph: dict) -> dict:
    """Format model-level DAG graph for API response (without node metadata)."""
    return ModelLineageResponse(
        graph={k: list(v) for k, v in graph.items()}
    ).model_dump(exclude_none=True)


def format_lineage_with_nodes(graph: dict, nodes: dict) -> dict:
    """Format model-level DAG graph with per-node metadata for card rendering.

    Args:
        graph: adjacency map {fqn: [upstream_deps]}
        nodes: per-node metadata {fqn: {name, fqn, type, kind, columns}}
    """
    from src.api.models.responses import LineageNode as LineageNodeModel

    formatted_nodes = {}
    for fqn, node_data in nodes.items():
        formatted_nodes[fqn] = LineageNodeModel(**node_data)

    return ModelLineageResponse(
        graph={k: list(v) for k, v in graph.items()},
        nodes=formatted_nodes,
    ).model_dump(exclude_none=True)


def format_column_lineage(adjacency: dict) -> dict:
    """Format column-level lineage adjacency list for API response."""
    return ColumnLineageResponse(lineage=adjacency).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Query / Evaluate serialization
# ---------------------------------------------------------------------------

def format_query_result(columns: list, rows: list, execution_time_ms: int) -> dict:
    """Format SQL query result."""
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": len(rows),
        "execution_time_ms": execution_time_ms,
    }


# ---------------------------------------------------------------------------
# Environment serialization
# ---------------------------------------------------------------------------

def format_environments(context) -> dict:
    """Format environment list from context state reader."""
    from sqlmesh.core import constants as c

    environments = {}
    try:
        for env in context.state_reader.get_environments():
            environments[env.name] = EnvironmentSummary(
                name=env.name,
                snapshots=[s.name for s in env.snapshots] if env.snapshots else [],
                start_at=str(env.start_at) if env.start_at else "",
                plan_id=env.plan_id or "",
                expiration_timestamp=env.expiration_timestamp if hasattr(env, "expiration_timestamp") else None,
                finalized_ts=env.finalized_ts if hasattr(env, "finalized_ts") else None,
            )
    except Exception as e:
        logger.warning("format_environments_failed", error=str(e))

    # Ensure prod and default target exist
    if c.PROD not in environments:
        environments[c.PROD] = EnvironmentSummary(name=c.PROD)
    default_target = context.config.default_target_environment if hasattr(context, "config") else c.PROD
    if default_target not in environments:
        environments[default_target] = EnvironmentSummary(name=default_target)

    return EnvironmentsResponse(
        environments=environments,
        pinned_environments=context.config.pinned_environments if hasattr(context, "config") else set(),
        default_target_environment=default_target,
    ).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Table Diff serialization
# ---------------------------------------------------------------------------

def format_table_diff(context, source: str, target: str, on: str = "",
                      model_or_snapshot: str = "", where: str = "",
                      temp_schema: str = "", limit: int = 20) -> dict:
    """Format table diff result."""
    from sqlglot import exp

    table_diffs = context.table_diff(
        source=source,
        target=target,
        on=exp.condition(on) if on else None,
        select_models={model_or_snapshot} if model_or_snapshot else None,
        where=where,
        limit=limit,
        show=False,
    )
    if not table_diffs:
        return None

    diff = table_diffs[0] if isinstance(table_diffs, list) else table_diffs
    _schema_diff = diff.schema_diff()

    schema_diff = SchemaDiff(
        source=_schema_diff.source,
        target=_schema_diff.target,
        source_schema={k: str(v) for k, v in _schema_diff.source_schema.items()},
        target_schema={k: str(v) for k, v in _schema_diff.target_schema.items()},
        added={k: str(v) for k, v in _schema_diff.added.items()},
        removed={k: str(v) for k, v in _schema_diff.removed.items()},
        modified={k: str(v) for k, v in _schema_diff.modified.items()},
    )

    _row_diff = diff.row_diff(temp_schema=temp_schema if temp_schema else None)
    row_diff = RowDiffSummary(
        source_count=_row_diff.source_count,
        target_count=_row_diff.target_count,
        count_pct_change=_row_diff.count_pct_change,
        stats=_row_diff.stats if hasattr(_row_diff, "stats") else {},
    )

    s_index, t_index, _ = diff.key_columns
    on_keys = [[s.name, t.name] for s, t in zip(s_index, t_index)]

    return TableDiffResult(
        schema_diff=schema_diff,
        row_diff=row_diff,
        on=on_keys,
    ).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Schema discovery serialization
# ---------------------------------------------------------------------------

def format_schema_tables(tables: list) -> dict:
    """Format schema discovery result."""
    return {"tables": tables}


# ---------------------------------------------------------------------------
# Test result serialization
# ---------------------------------------------------------------------------

def format_test_result(result, context_path: str = "") -> dict:
    """Format test result from SQLMesh test run."""
    from sqlmesh.core.test import ModelTest

    def _test_path(test) -> t.Optional[str]:
        path = test.path_relative_to(context_path) if hasattr(test, "path_relative_to") else None
        return str(path.as_posix()) if path else None

    errors = []
    for test, tb in result.errors:
        test = t.cast(ModelTest, test)
        errors.append({"name": test.test_name, "path": _test_path(test), "tb": tb})

    failures = []
    for test, tb in result.failures:
        test = t.cast(ModelTest, test)
        failures.append({"name": test.test_name, "path": _test_path(test), "tb": tb})

    skipped = []
    for test, reason in result.skipped:
        test = t.cast(ModelTest, test)
        skipped.append({"name": test.test_name, "path": _test_path(test), "reason": reason})

    successes = []
    for test in result.successes:
        test = t.cast(ModelTest, test)
        successes.append({"name": test.test_name, "path": _test_path(test)})

    return {
        "tests_run": result.testsRun,
        "errors": errors,
        "failures": failures,
        "skipped": skipped,
        "successes": successes,
    }


def format_test_failure_summary(result, dialect: str = "") -> dict:
    """Build a structured test failure summary for the plan UI.

    Mirrors the SQLMesh web server's ReportTestsFailure payload so the
    frontend can render a rich "One or More Tests Failed" panel with totals
    and per-test tracebacks.

    Args:
        result: A ModelTextTestResult from context.test().
        dialect: The dialect the tests ran against (e.g. "duckdb").
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


def format_lint_violations(violations: t.Iterable) -> t.Dict[str, t.Any]:
    """Convert a list of SQLMesh ``AnnotatedRuleViolation`` to a UI-friendly dict.

    The shape mirrors the SQLMesh CLI's per-file output so the frontend can
    show a familiar "rule: message" line plus the offending file. Tolerant of
    missing attributes (older SQLMesh versions, partial violations) so a
    single bad record never breaks the whole plan.
    """
    by_file: t.List[t.Dict[str, t.Any]] = []
    errors = 0
    warnings = 0
    seen_keys: t.Set[tuple] = set()

    for v in violations:
        try:
            rule_name = getattr(getattr(v, "rule", None), "name", "") or ""
            message = getattr(v, "violation_msg", "") or ""
            model = getattr(v, "model", None)
            model_name = (
                getattr(model, "name", "")
                or getattr(model, "_path", "")
                or ""
            )
            file_path = ""
            if model is not None:
                p = getattr(model, "_path", None)
                if p is not None:
                    try:
                        file_path = str(p)
                    except Exception:
                        file_path = ""
            severity = getattr(v, "violation_type", "error") or "error"
            line = None
            column = None
            rng = getattr(v, "violation_range", None)
            start = getattr(rng, "start", None) if rng is not None else None
            if start is not None:
                # ``start`` is a ``Range.Position`` with ``line`` (0-based) and ``column`` (0-based).
                line = getattr(start, "line", None)
                if line is not None:
                    line = int(line) + 1  # 1-based for display
                col = getattr(start, "column", None)
                if col is not None:
                    column = int(col) + 1
        except Exception:
            # If we cannot read this violation at all, still surface a minimal entry.
            rule_name, message, model_name, file_path, severity = "", str(v), "", "", "error"
            line = column = None

        if severity == "error":
            errors += 1
        elif severity == "warning":
            warnings += 1

        # Deduplicate identical entries (a rule can fire twice on the same model).
        key = (file_path, model_name, rule_name, message, severity, line, column)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        by_file.append({
            "model_name": model_name,
            "file_path": file_path,
            "rule": rule_name,
            "message": message,
            "severity": severity,
            "line": line,
            "column": column,
        })

    # Build a human-readable report that mirrors the native SQLMesh CLI output,
    # e.g.:
    #   Linter errors for /path/to/models/foo.sql:
    #    - somerule: Unknown column: bar for model '"db"."foo"', ...
    # Violations are grouped by file then severity so the text matches what a
    # user would see running `sqlmesh plan` locally.
    report = _build_lint_report(by_file)

    return {
        "total": len(by_file),
        "errors": errors,
        "warnings": warnings,
        "by_file": by_file,
        "report": report,
        "message": (
            f"{errors} lint error(s), {warnings} warning(s) across "
            f"{len({v['file_path'] for v in by_file if v['file_path']})} file(s)"
            if by_file
            else ""
        ),
    }


def _build_lint_report(by_file: t.List[t.Dict[str, t.Any]]) -> str:
    """Render lint violations as native-CLI-style grouped text.

    Groups violations by (file_path, severity) and formats each block with a
    ``Linter errors for <file>:`` / ``Linter warnings for <file>:`` header,
    followed by one ``- [Line N: ]<rule>: <message>`` bullet per violation.
    """
    # Preserve first-seen file ordering while grouping by severity within file.
    grouped: "OrderedDict[tuple, t.List[t.Dict[str, t.Any]]]" = OrderedDict()
    for v in by_file:
        key = (v.get("file_path") or "", v.get("severity") or "error")
        grouped.setdefault(key, []).append(v)

    blocks: t.List[str] = []
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
