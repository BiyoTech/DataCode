"""Sandbox-side implementations of the read/execute ops (P6 close-out).

Everything here runs INSIDE the executor sandbox, where the user's Python
models and macros have already been exec'd by the Context constructor —
which is exactly why this code must live here and not in the runtime process
(threat T2: the platform process holds DB/LLM/encryption credentials).

Contract: every function receives the sandbox's Context and returns PURE DATA
(dicts / lists / strings). No SQLGlot AST node, engine connection or live
model object may cross the socket — that reduction is the whole point of the
metadata split (design 04 §3.7). The returned dicts are the final API shapes
the REST layer has always produced, so the runtime side only caches and
forwards them.

Freshness: each read op calls ``context.refresh()`` first. That is SQLMesh's
own cheap mtime check over files tracked at load time — a no-op when nothing
changed, and the same semantics the in-process ContextPool applied at these
call sites before the split. Add/remove of files still relies on the
runtime's explicit dirty marking (``context.refresh`` op), which the
ExecutorManager applies before the next op.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Set

from sqlglot import exp

from src.executor.op_error import ERR_MODEL_NOT_FOUND, OpError

# SQLMesh project sub-directory of a working copy. Mirrors server.py's own
# constant; restated (not imported) so this module keeps the property of
# importing cleanly from a bare interpreter.
_SQL_WAREHOUSE_DIR = "sql-warehouse"


def _workspace_path() -> str:
    return os.environ.get("DATACODE_WORKSPACE_PATH", "")


def _sqlmesh_root() -> str:
    """The SQLMesh project directory inside the mounted working copy."""
    warehouse = os.path.join(_workspace_path(), _SQL_WAREHOUSE_DIR)
    if os.path.isdir(warehouse):
        return warehouse
    return _workspace_path()


def _refresh(context) -> None:
    """Pick up external on-disk edits before reading metadata.

    Cheap by design: SQLMesh compares each tracked file's mtime against the
    value captured at load time and only reloads what actually changed.
    """
    context.refresh()


def _get_model(context, model_name: str):
    """Fetch a model or raise the shared not-found op error."""
    model = context.get_model(model_name, raise_if_missing=False)
    if not model:
        raise OpError(ERR_MODEL_NOT_FOUND, f"model not found: {model_name}")
    return model


# ── models / metrics ────────────────────────────────────────────────────────


def op_models(context, params: dict) -> dict:
    """List models (local definition view or a promoted environment's).

    params:
        environment      – promoted environment name; empty/local means the
                           on-disk definition view
        include_metrics  – also return the project's METRIC names (used by the
                           ontology service to validate semantic-layer metrics)
    """
    _refresh(context)

    from src.sqlmesh.environment_topology import (
        build_environment_model_items,
        is_local_environment,
    )
    from src.sqlmesh.format import format_model_list_item

    environment = params.get("environment") or None
    result: Dict[str, Any] = {}

    if environment and not is_local_environment(context, environment):
        models = build_environment_model_items(context, environment)
    else:
        models = [format_model_list_item(m) for m in (context.models or {}).values()]

    models.sort(key=lambda m: m.get("fqn", m.get("name", "")))
    result["models"] = models

    if params.get("include_metrics"):
        names = sorted((context.metrics or {}).keys())
        result["metrics"] = [{"name": n} for n in names]

    return result


def op_model_detail(context, params: dict) -> dict:
    """Full detail for one model, rendered SQL included.

    Rendering executes the user's macros — that is why this op exists.
    """
    _refresh(context)

    from src.sqlmesh.format import format_model_detail

    model = _get_model(context, params["model"])
    return format_model_detail(model, context=context, render_query=True)


def op_metrics(context, params: dict) -> dict:
    """The project's METRIC names (flat identifiers, sorted)."""
    _refresh(context)
    names = sorted((context.metrics or {}).keys())
    return {"metrics": [{"name": n} for n in names]}


# ── environments ────────────────────────────────────────────────────────────


def op_environments(context, params: dict) -> dict:
    """The engine-side environment view.

    params:
        raw_names – return the names that ACTUALLY exist in state, without
                    synthesizing prod / the default target. Reconciliation
                    needs the raw truth; a UI needs the friendly view.
    """
    _refresh(context)

    if params.get("raw_names"):
        names = [env.name for env in context.state_reader.get_environments()]
        return {"names": names}

    from src.sqlmesh.format import format_environments

    return format_environments(context)


# ── lineage (model level) ───────────────────────────────────────────────────


def op_lineage(context, params: dict) -> dict:
    """Model dependency DAG, focused or full, local or environment-scoped.

    params:
        model       – focus the subgraph on this model's ancestors + descendants
        environment – derive the topology from a promoted environment's
                      snapshots instead of the local definition
        column      – when set, switch to COLUMN-level lineage (see
                      op_column_lineage semantics); kept on this op because
                      model/column lineage is one feature to the caller
        models_only – with ``column``: model-level dependencies only
    """
    if params.get("column"):
        return _op_column_lineage(context, params)

    _refresh(context)

    from src.sqlmesh.environment_topology import (
        build_environment_graph_with_nodes,
        is_local_environment,
        resolve_model_fqn_in_environment,
    )
    from src.sqlmesh.format import format_lineage_with_nodes

    model_name = params.get("model") or ""
    environment = params.get("environment") or None

    if environment and not is_local_environment(context, environment):
        graph, nodes = build_environment_graph_with_nodes(context, environment)
        if model_name:
            focus_fqn = resolve_model_fqn_in_environment(context, nodes, model_name)
            if not focus_fqn:
                raise OpError(ERR_MODEL_NOT_FOUND, f"model not found: {model_name}")
            graph, nodes = _focus_prebuilt_graph(graph, nodes, focus_fqn)
        return format_lineage_with_nodes(graph, nodes)

    if model_name:
        fqn = _get_model(context, model_name).fqn
        graph, nodes = _build_focused_subgraph_with_nodes(context, fqn)
    else:
        graph, nodes = _build_full_graph_with_nodes(context)
    return format_lineage_with_nodes(graph, nodes)


# ── lineage (column level) ──────────────────────────────────────────────────


def _op_column_lineage(context, params: dict) -> dict:
    """Column-level lineage as an adjacency list.

    Rendering the traced column's expression executes the user's macros
    (Python models derive their columns only through rendering), so this is
    an execute-class op despite living under the metadata prefix.
    """
    _refresh(context)

    from src.sqlmesh.format import format_column_lineage

    model_name = params["model"]
    column_name = params["column"]
    models_only = bool(params.get("models_only"))

    fqn = _get_model(context, model_name).fqn

    if models_only:
        adjacency = _models_only_upstream_adjacency(fqn, column_name, context)
        downstream = _models_only_downstream_adjacency(fqn, column_name, context)
        _merge_adjacency(adjacency, downstream)
    else:
        adjacency = _column_lineage_adjacency(fqn, column_name, context)

    return format_column_lineage(adjacency)


def _quote_column(column: str, dialect: str) -> str:
    return exp.to_identifier(column, quoted=True).sql(dialect=dialect)


def _get_source_name(node, default_catalog, dialect, model_name):
    """Get the source table name for a lineage node."""
    table = node.expression.find(exp.Table)
    if table:
        from sqlmesh.core.dialect import normalize_model_name

        return normalize_model_name(table, default_catalog=default_catalog, dialect=dialect)
    if node.reference_node_name:
        return f"{model_name}: {node.reference_node_name}"
    return ""


def _get_column_name(node):
    """Get the output column name for a lineage node."""
    if isinstance(node.expression, exp.Alias):
        return node.expression.alias_or_name
    return exp.to_column(node.name).name


def _column_lineage_adjacency(model_name, column_name, context):
    """Adjacency list with CTE details (matches SQLMesh web server)."""
    from sqlmesh.core.lineage import lineage

    from src.api.models.responses import LineageColumn as LineageColumnModel

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    visited = {(model_name, column_name)}
    nodes = [(model_name, column_name)]

    while nodes:
        current_model, current_column = nodes.pop(0)
        model = context.get_model(current_model)
        if not model:
            graph[current_model][current_column] = LineageColumnModel(
                expression=f"FROM {current_model}",
                source=f"SELECT {current_column} FROM {current_model}",
                models={},
            ).model_dump()
            continue

        root = lineage(_quote_column(current_column, model.dialect), model)

        for node in root.walk():
            if root.name == "UNION" and node is root:
                continue

            node_name = (
                _get_source_name(
                    node,
                    default_catalog=context.default_catalog,
                    dialect=model.dialect,
                    model_name=current_model,
                )
                or current_model
            )
            node_column = _get_column_name(node)

            if node_column in graph.get(node_name, {}):
                dependencies = defaultdict(
                    set, graph[node_name][node_column].get("models", {})
                )
            else:
                dependencies = defaultdict(set)

            for d in node.downstream:
                table = _get_source_name(
                    d,
                    default_catalog=context.default_catalog,
                    dialect=model.dialect,
                    model_name=current_model,
                )
                if table:
                    dep_column = _get_column_name(d)
                    dependencies[table].add(dep_column)
                    if (
                        isinstance(d.expression, exp.Table)
                        and (table, dep_column) not in visited
                    ):
                        nodes.append((table, dep_column))
                        visited.add((table, dep_column))

            graph[node_name][node_column] = LineageColumnModel(
                expression=node.expression.sql(pretty=True, dialect=model.dialect),
                source=node.source.sql(pretty=True, dialect=model.dialect),
                models=dict(dependencies),
            ).model_dump()

    return dict(graph)


def _norm_fqn(name: str) -> str:
    """Normalize an FQN for tolerant comparison (strip quotes, lower-case)."""
    return (name or "").replace('"', "").lower()


def _merge_adjacency(
    base: Dict[str, Dict[str, dict]], extra: Dict[str, Dict[str, dict]]
) -> Dict[str, Dict[str, dict]]:
    """Merge ``extra`` column-lineage adjacency into ``base`` in place."""
    for model_fqn, columns in extra.items():
        base_cols = base.setdefault(model_fqn, {})
        for col_name, data in columns.items():
            if col_name in base_cols:
                existing_models = base_cols[col_name].setdefault("models", {})
                for up_fqn, up_cols in data.get("models", {}).items():
                    merged = set(existing_models.get(up_fqn, [])) | set(up_cols)
                    existing_models[up_fqn] = sorted(merged)
            else:
                base_cols[col_name] = data
    return base


def _models_only_downstream_adjacency(model_name, column_name, context):
    """Downstream adjacency with model-level dependencies only.

    Traverses descendant models and records every downstream column that
    depends on the traced column. Same shape as the upstream helper so the
    client can render both directions uniformly.
    """
    from sqlmesh.core.lineage import column_dependencies

    from src.api.models.responses import LineageColumn as LineageColumnModel

    children_map: Dict[str, Set[str]] = defaultdict(set)
    for child, parents in context.dag.graph.items():
        for parent in parents:
            children_map[parent].add(child)

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    queue: List[tuple] = [(model_name, column_name)]
    visited = {(model_name, column_name)}

    while queue:
        current_model, current_column = queue.pop(0)
        for child in children_map.get(current_model, set()):
            child_model = context.get_model(child)
            if not child_model:
                continue

            for child_col in (child_model.columns_to_types or {}):
                deps = column_dependencies(
                    context, child, _quote_column(child_col, child_model.dialect)
                )
                depends = any(
                    _norm_fqn(dep_table) == _norm_fqn(current_model)
                    and current_column in dep_cols
                    for dep_table, dep_cols in deps.items()
                )
                if not depends:
                    continue

                entry = graph[child].get(child_col)
                if entry:
                    models = entry.setdefault("models", {})
                    cols = models.setdefault(current_model, [])
                    if current_column not in cols:
                        cols.append(current_column)
                else:
                    graph[child][child_col] = LineageColumnModel(
                        models={current_model: [current_column]},
                    ).model_dump()

                if (child, child_col) not in visited:
                    visited.add((child, child_col))
                    queue.append((child, child_col))

    return dict(graph)


def _models_only_upstream_adjacency(model_name, column_name, context):
    """Upstream adjacency with model-level dependencies only."""
    from sqlmesh.core.lineage import column_dependencies

    from src.api.models.responses import LineageColumn as LineageColumnModel

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    nodes = [(model_name, column_name)]
    visited = {(model_name, column_name)}

    while nodes:
        current_model, current_column = nodes.pop(0)
        model = context.get_model(current_model)
        dependencies = defaultdict(set)

        if model:
            for table, column_names in column_dependencies(
                context, current_model, _quote_column(current_column, model.dialect)
            ).items():
                for col_name in column_names:
                    if (table, col_name) not in visited:
                        dependencies[table].add(col_name)
                        nodes.append((table, col_name))
                        visited.add((table, col_name))

        graph[current_model][current_column] = LineageColumnModel(
            models=dict(dependencies),
        ).model_dump()

    return dict(graph)


# ── subgraph builders (model level) ────────────────────────────────────────


def _focus_prebuilt_graph(
    graph: Dict[str, List[str]],
    nodes: Dict[str, Dict[str, Any]],
    focus_fqn: str,
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Reduce an already-built adjacency graph to a focused subgraph.

    Collects all upstream ancestors and downstream descendants of
    ``focus_fqn`` and keeps only the edges between the retained nodes.
    """
    upstream: Set[str] = set()
    queue: deque = deque([focus_fqn])
    seen = {focus_fqn}
    while queue:
        current = queue.popleft()
        for parent in graph.get(current, []):
            if parent not in seen:
                seen.add(parent)
                upstream.add(parent)
                queue.append(parent)

    children_map: Dict[str, Set[str]] = defaultdict(set)
    for node, parents in graph.items():
        for parent in parents:
            children_map[parent].add(node)

    downstream: Set[str] = set()
    queue = deque([focus_fqn])
    seen = {focus_fqn}
    while queue:
        current = queue.popleft()
        for child in children_map.get(current, set()):
            if child not in seen:
                seen.add(child)
                downstream.add(child)
                queue.append(child)

    retained = upstream | downstream | {focus_fqn}
    focused_graph = {
        node: [p for p in graph.get(node, []) if p in retained] for node in retained
    }
    focused_nodes = {fqn: meta for fqn, meta in nodes.items() if fqn in retained}
    return focused_graph, focused_nodes


def _build_focused_subgraph_with_nodes(
    context, fqn: str
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Focused subgraph (upstream + downstream) plus node metadata."""
    graph = _build_focused_subgraph(context, fqn)
    nodes = _build_lineage_nodes(context, list(graph.keys()))
    return graph, nodes


def _build_full_graph_with_nodes(
    context,
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """The full project DAG with lightweight per-node metadata."""
    full_graph: Dict[str, Set[str]] = context.dag.graph
    graph = {node: sorted(deps) for node, deps in full_graph.items()}
    nodes = _build_lineage_nodes(context, list(full_graph.keys()))
    return graph, nodes


def _build_focused_subgraph(context, fqn: str) -> Dict[str, List[str]]:
    """Focused subgraph containing upstream ancestors and downstream descendants.

    The returned adjacency map preserves the DAG edge direction semantics:
    value = list of upstream dependencies (parents) for each node key.
    """
    full_graph: Dict[str, Set[str]] = context.dag.graph
    upstream_graph: Dict[str, Set[str]] = context.dag.lineage(fqn).graph

    upstream_nodes: Set[str] = set(upstream_graph.keys())
    for parents in upstream_graph.values():
        upstream_nodes.update(parents)

    downstream_nodes: Set[str] = set()
    downstream_graph: Dict[str, Set[str]] = defaultdict(set)

    queue: deque = deque([fqn])
    visited_downstream: Set[str] = {fqn}

    while queue:
        current = queue.popleft()
        for node, parents in full_graph.items():
            if current in parents and node not in visited_downstream:
                visited_downstream.add(node)
                queue.append(node)
                downstream_nodes.add(node)

    for node in downstream_nodes | {fqn}:
        parents = full_graph.get(node, set())
        relevant_parents = parents & (downstream_nodes | {fqn} | upstream_nodes)
        if relevant_parents:
            downstream_graph[node] = relevant_parents

    merged: Dict[str, Set[str]] = defaultdict(set)
    for node, parents in upstream_graph.items():
        merged[node] |= parents
    for node, parents in downstream_graph.items():
        merged[node] |= parents

    all_nodes = upstream_nodes | downstream_nodes | {fqn}
    for node in all_nodes:
        if node not in merged:
            merged[node] = set()

    return {node: sorted(deps) for node, deps in merged.items()}


def _build_lineage_nodes(context, fqns: List[str]) -> Dict[str, Dict[str, Any]]:
    """Lightweight node metadata for lineage card rendering.

    Does NOT trigger render_query or column_description (expensive); external
    models get an empty columns list.
    """
    from src.api.models.responses import LineageNode as LineageNodeModel

    nodes: Dict[str, Dict[str, Any]] = {}
    for fqn in fqns:
        model = context.get_model(fqn, raise_if_missing=False)
        if model:
            columns = []
            for col_name, data_type in (model.columns_to_types or {}).items():
                columns.append({"name": col_name, "type": str(data_type)})

            kind = getattr(model, "kind", None)
            kind_name = kind.name if hasattr(kind, "name") else (str(kind) if kind else "")
            source_type = getattr(model, "source_type", None)
            model_type = str(source_type).lower() if source_type else "sql"

            nodes[fqn] = LineageNodeModel(
                name=model.name,
                fqn=model.fqn,
                type=model_type,
                kind=kind_name,
                columns=columns,
            ).model_dump()
        else:
            short_name = fqn.split(".")[-1] if "." in fqn else fqn
            nodes[fqn] = LineageNodeModel(
                name=short_name,
                fqn=fqn,
                type="external",
                kind="",
                columns=[],
            ).model_dump()

    return nodes


# ── query / evaluate ────────────────────────────────────────────────────────


def _apply_safe_limit(sql: str, limit: int, max_limit: int) -> str:
    """Safely apply LIMIT to SQL using sqlglot (no string concatenation).

    The limit is a validated int by the time it arrives; parsing simply keeps
    it out of string surgery. Falls back to a plain append when the SQL does
    not parse (still safe: the limit is an integer).
    """
    if not isinstance(limit, int) or limit <= 0:
        limit = max_limit
    limit = min(limit, max_limit)

    try:
        from sqlglot import parse_one
        from sqlglot.expressions import Limit

        expr = parse_one(sql)
        if not expr.find(Limit):
            expr = expr.limit(limit)
        return expr.sql()
    except Exception:
        clean_sql = sql.rstrip().rstrip(";")
        if "LIMIT" not in clean_sql.upper().split("\n")[-1]:
            return f"{clean_sql} LIMIT {limit}"
        return clean_sql


def _df_to_query_payload(df, execution_time_ms: int, *, keep_native_values: bool) -> dict:
    """Convert a pandas DataFrame to the wire query-result shape.

    ``keep_native_values`` preserves ints/floats/bools (evaluate contract);
    the ad-hoc query endpoint stringifies everything (its historical shape).
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        df = df.toPandas()

    columns = list(df.columns)
    if keep_native_values:
        df = df.replace({pd.NA: None}).where(df.notna(), None)
        rows = [
            [
                None if pd.isna(v) else v if isinstance(v, (int, float, bool)) else str(v)
                for v in row
            ]
            for _, row in df.iterrows()
        ]
    else:
        rows = [
            [str(v) if v is not None else "" for v in row]
            for _, row in df.iterrows()
        ]

    return {
        "columns": columns,
        "rows": rows,
        "total_rows": len(rows),
        "execution_time_ms": execution_time_ms,
    }


def op_query(context, params: dict) -> dict:
    """Ad-hoc SQL execution with a safe LIMIT.

    The runtime passes the row limit policy (default + max); the sandbox
    applies it so the limit is enforced regardless of the driver.
    """
    sql = params["sql"]
    row_limit = int(params.get("row_limit") or 0)
    max_limit = int(params.get("max_row_limit") or row_limit or 1000)

    safe_sql = _apply_safe_limit(sql, row_limit, max_limit)
    start_time = time.time()
    df = context.fetchdf(safe_sql)
    return _df_to_query_payload(
        df, int((time.time() - start_time) * 1000), keep_native_values=False
    )


def op_evaluate(context, params: dict) -> dict:
    """Evaluate a model and return its rows.

    External models (schema-only stubs) are queried directly against their
    physical table: ``context.evaluate`` would render CAST(NULL AS ...) for
    every column of a stub.
    """
    model_name = params["model"]
    limit = int(params.get("limit") or 1000)

    start_time = time.time()

    m = context.get_model(model_name, raise_if_missing=False)
    if m is not None and getattr(m, "source_type", None) == "external":
        table = exp.to_table(m.fqn)
        query = exp.select("*").from_(table, copy=False).limit(limit)
        df = context.engine_adapter.fetchdf(query)
    else:
        df = context.evaluate(
            model_name,
            start=params.get("start"),
            end=params.get("end"),
            execution_time=params.get("execution_time"),
            limit=limit,
        )

    return _df_to_query_payload(
        df, int((time.time() - start_time) * 1000), keep_native_values=True
    )


# ── render ──────────────────────────────────────────────────────────────────


def op_render(context, params: dict) -> dict:
    """Render a model's SQL with macro expansion.

    params:
        model, start, end, execution_time, expand, pretty, dialect
    """
    _refresh(context)

    snapshot = context.get_snapshot(params["model"], raise_if_missing=False)
    if not snapshot:
        raise OpError(ERR_MODEL_NOT_FOUND, f"model not found: {params['model']}")

    rendered = context.render(
        snapshot,
        start=params.get("start"),
        end=params.get("end"),
        execution_time=params.get("execution_time"),
        expand=bool(params.get("expand")),
    )

    effective_dialect = params.get("dialect") or context.config.dialect
    pretty = params.get("pretty")
    sql = rendered.sql(
        pretty=True if pretty is None else bool(pretty),
        dialect=effective_dialect,
    )
    return {"sql": sql, "dialect": effective_dialect}


# ── tests ───────────────────────────────────────────────────────────────────


def op_test(context, params: dict) -> dict:
    """Run the project's unit tests.

    params:
        detailed  – rich result (per-test outcome with tracebacks) for the
                    command endpoint; the plan gate keeps the compact summary
        test       – restrict to one test path, relative to the project root
        verbosity  – "default" | "verbose" | "quiet"
    """
    from src.sqlmesh.plan_engine import run_tests_summary

    if not params.get("detailed"):
        summary = run_tests_summary(context)
        return summary if summary is not None else {"total": 0, "failures": 0, "errors": 0}

    from sqlmesh.core.console import Verbosity

    from src.sqlmesh.format import format_test_result

    verbosity = params.get("verbosity") or "default"
    verbosity_enum = Verbosity.DEFAULT
    if verbosity == "verbose":
        verbosity_enum = Verbosity.VERBOSE
    elif verbosity == "quiet":
        verbosity_enum = Verbosity.QUIET

    test_paths = None
    if params.get("test"):
        test_paths = [str(os.path.join(_sqlmesh_root(), params["test"]))]

    result = context.test(tests=test_paths, verbosity=verbosity_enum)
    return format_test_result(result, context_path=_workspace_path())


# ── table diff ──────────────────────────────────────────────────────────────


def op_table_diff(context, params: dict) -> dict:
    """Differences between two environments' tables.

    Rendering the compared models executes the user's macros, which is why
    this runs in the sandbox (new op, additive; see the protocol's migration
    log).
    """
    _refresh(context)

    from src.sqlmesh.format import format_table_diff

    result = format_table_diff(
        context,
        source=params["source"],
        target=params["target"],
        on=params.get("on") or "",
        model_or_snapshot=params.get("model_or_snapshot") or "",
        where=params.get("where") or "",
        temp_schema=params.get("temp_schema") or "",
        limit=int(params.get("limit") or 20),
    )
    # format_table_diff returns None when there is nothing to compare; the
    # wire shape keeps that explicit rather than collapsing it into {}.
    return {"result": result}
