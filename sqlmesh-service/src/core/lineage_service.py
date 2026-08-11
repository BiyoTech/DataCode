"""Lineage service - model and column dependency DAG operations.

Matches SQLMesh web server's lineage endpoint behavior:
- Model-level lineage (parent/child graph) with focused upstream+downstream subgraph
- Column-level lineage with adjacency list
- Per-node metadata (name, fqn, type, kind, columns) for rich card rendering
"""

import asyncio
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set

from sqlglot import exp

from src.core.context_pool import get_context_pool
from src.core.environment_topology import (
    build_environment_graph_with_nodes,
    is_local_environment,
    resolve_model_fqn_in_environment,
)
from src.core.format import format_column_lineage, format_lineage_with_nodes
from src.utils.errors import ErrorCode, ModelNotFoundError, service_error_from_exception
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class LineageService:
    """Lineage read operations using SQLMesh SDK."""

    async def get_lineage(
        self,
        workspace_path: str,
        model_name: str = "",
        gateway: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get model dependency DAG.

        If model_name is provided, returns a focused subgraph containing both
        upstream ancestors and downstream descendants of the given model,
        along with per-node metadata (type, kind, columns).

        Otherwise returns the full project DAG without node metadata.

        When ``environment`` names a promoted SQLMesh virtual environment (e.g.
        ``prod``/``dev``), the topology is derived from that environment's
        snapshots instead of the local project definition. When it is empty or
        refers to the local definition view, the in-memory ``context.dag`` is
        used (the previous behavior).
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            # Environment-scoped topology: build the graph from the promoted
            # environment's snapshots so the lineage reflects what is actually
            # deployed to prod/dev rather than the local (unplanned) definition.
            if not is_local_environment(entry.context, environment):
                graph, nodes = await asyncio.to_thread(
                    build_environment_graph_with_nodes,
                    entry.context,
                    environment,
                )
                if model_name:
                    focus_fqn = resolve_model_fqn_in_environment(
                        entry.context, nodes, model_name
                    )
                    if not focus_fqn:
                        raise ModelNotFoundError(model_name)
                    graph, nodes = _focus_prebuilt_graph(graph, nodes, focus_fqn)
                return format_lineage_with_nodes(graph, nodes)

            if model_name:
                model = await asyncio.to_thread(
                    entry.context.get_model, model_name, raise_if_missing=False
                )
                if not model:
                    raise ModelNotFoundError(model_name)

                fqn = model.fqn
                # Build focused subgraph (upstream + downstream) and node metadata
                # in a worker thread to avoid blocking the event loop.
                graph, nodes = await asyncio.to_thread(
                    _build_focused_subgraph_with_nodes, entry.context, fqn
                )
                return format_lineage_with_nodes(graph, nodes)
            else:
                # Full project DAG. Also build lightweight node metadata so that
                # downstream consumers (e.g. the Dagster bridge) can resolve each
                # FQN to its display ``name`` without a second round trip.
                graph, nodes = await asyncio.to_thread(
                    _build_full_graph_with_nodes, entry.context
                )
                return format_lineage_with_nodes(graph, nodes)
        except ModelNotFoundError:
            raise
        except Exception as e:
            logger.error("get_lineage_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to get lineage",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="lineage.get",
                    gateway=gateway,
                    model_fqn=model_name or None,
                ),
            ) from e

    async def get_column_lineage(
        self,
        workspace_path: str,
        model_name: str,
        column_name: str,
        models_only: bool = False,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get column-level lineage as adjacency list.

        Matches SQLMesh web server's GET /lineage/{model}/{column} behavior.
        Uses SQLMesh's lineage module to trace column dependencies.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            model = await asyncio.to_thread(
                entry.context.get_model, model_name, raise_if_missing=False
            )
            if not model:
                raise ModelNotFoundError(model_name)

            fqn = model.fqn
            if models_only:
                # Upstream: columns the traced column depends on.
                adjacency = await asyncio.to_thread(
                    _create_models_only_lineage_adjacency_list,
                    fqn, column_name, entry.context,
                )
                # Downstream: columns that depend on the traced column. Merged
                # into the same adjacency shape so the client can draw both
                # upstream and downstream connections uniformly.
                downstream = await asyncio.to_thread(
                    _create_models_only_downstream_adjacency_list,
                    fqn, column_name, entry.context,
                )
                _merge_adjacency(adjacency, downstream)
            else:
                adjacency = await asyncio.to_thread(
                    _create_lineage_adjacency_list,
                    fqn, column_name, entry.context,
                )

            return format_column_lineage(adjacency)
        except ModelNotFoundError:
            raise
        except Exception as e:
            logger.error("get_column_lineage_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to get column lineage",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="lineage.get_column",
                    gateway=gateway,
                    model_fqn=model_name,
                    context={"column_name": column_name},
                ),
            ) from e


# ---------------------------------------------------------------------------
# Prebuilt-graph focusing (environment-scoped topology)
# ---------------------------------------------------------------------------

def _focus_prebuilt_graph(
    graph: Dict[str, List[str]],
    nodes: Dict[str, Dict[str, Any]],
    focus_fqn: str,
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Reduce an already-built adjacency graph to a focused subgraph.

    Unlike :func:`_build_focused_subgraph`, this operates on a precomputed
    adjacency map (``{fqn: [upstream_deps]}``) rather than ``context.dag``, so
    it can focus a topology derived from an environment's snapshots. Collects
    all upstream ancestors and downstream descendants of ``focus_fqn`` and keeps
    only the edges between the retained nodes.
    """
    # Upstream ancestors via forward BFS over the adjacency (node -> parents).
    upstream: Set[str] = set()
    queue: deque[str] = deque([focus_fqn])
    seen: Set[str] = {focus_fqn}
    while queue:
        current = queue.popleft()
        for parent in graph.get(current, []):
            if parent not in seen:
                seen.add(parent)
                upstream.add(parent)
                queue.append(parent)

    # Downstream descendants via reverse BFS (parent -> children).
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
    focused_graph: Dict[str, List[str]] = {}
    for node in retained:
        deps = [p for p in graph.get(node, []) if p in retained]
        focused_graph[node] = sorted(deps)

    focused_nodes = {fqn: meta for fqn, meta in nodes.items() if fqn in retained}
    return focused_graph, focused_nodes


# ---------------------------------------------------------------------------
# Focused subgraph helpers (upstream ancestors + downstream descendants)
# ---------------------------------------------------------------------------

def _build_focused_subgraph_with_nodes(
    context, fqn: str
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Build a focused subgraph (upstream + downstream) and node metadata.

    Returns a tuple of (adjacency_graph, nodes) where:
    - adjacency_graph: {fqn: [upstream_deps]} for all nodes in the subgraph
    - nodes: {fqn: {name, fqn, type, kind, columns}} for card rendering
    """
    graph = _build_focused_subgraph(context, fqn)
    nodes = _build_lineage_nodes(context, list(graph.keys()))
    return graph, nodes


def _build_full_graph_with_nodes(
    context,
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Build the full project DAG with lightweight per-node metadata.

    Returns a tuple of (adjacency_graph, nodes) where:
    - adjacency_graph: {fqn: [upstream_deps]} for every model in the project
    - nodes: {fqn: {name, fqn, type, kind, columns}} for name resolution

    The node metadata reuses ``_build_lineage_nodes`` (no render_query / column
    descriptions), so this stays O(model count) and cheap enough to run on the
    full DAG.
    """
    full_graph: Dict[str, Set[str]] = context.dag.graph
    graph = {node: sorted(deps) for node, deps in full_graph.items()}
    nodes = _build_lineage_nodes(context, list(full_graph.keys()))
    return graph, nodes


def _build_focused_subgraph(context, fqn: str) -> Dict[str, List[str]]:
    """Build a focused subgraph containing upstream ancestors and downstream descendants.

    The returned adjacency map preserves the DAG edge direction semantics:
    value = list of upstream dependencies (parents) for each node key.

    - Upstream: collected via ``context.dag.lineage(fqn).graph``
    - Downstream: reverse BFS over ``context.dag.graph`` to find all descendants
    """
    full_graph: Dict[str, Set[str]] = context.dag.graph

    # Upstream ancestors (includes the focal node itself)
    upstream_graph: Dict[str, Set[str]] = context.dag.lineage(fqn).graph

    # Collect all upstream node names
    upstream_nodes: Set[str] = set(upstream_graph.keys())
    for parents in upstream_graph.values():
        upstream_nodes.update(parents)

    # Downstream descendants via reverse BFS on the full DAG.
    # Build a reverse adjacency: child -> parents is the original; we need parent -> children.
    downstream_nodes: Set[str] = set()
    downstream_graph: Dict[str, Set[str]] = defaultdict(set)

    queue: deque[str] = deque([fqn])
    visited_downstream: Set[str] = {fqn}

    while queue:
        current = queue.popleft()
        for node, parents in full_graph.items():
            if current in parents and node not in visited_downstream:
                visited_downstream.add(node)
                queue.append(node)
                downstream_nodes.add(node)

    # Build the downstream adjacency (only edges among downstream nodes)
    for node in downstream_nodes | {fqn}:
        parents = full_graph.get(node, set())
        relevant_parents = parents & (downstream_nodes | {fqn} | upstream_nodes)
        if relevant_parents:
            downstream_graph[node] = relevant_parents

    # Merge upstream and downstream into a single adjacency map
    merged: Dict[str, Set[str]] = defaultdict(set)

    # Add upstream edges
    for node, parents in upstream_graph.items():
        merged[node] |= parents

    # Add downstream edges (may overlap with focal node)
    for node, parents in downstream_graph.items():
        merged[node] |= parents

    # Ensure every node in the subgraph has an entry (even if no parents)
    all_nodes = upstream_nodes | downstream_nodes | {fqn}
    for node in all_nodes:
        if node not in merged:
            merged[node] = set()

    # Convert sets to sorted lists for deterministic JSON output
    return {node: sorted(deps) for node, deps in merged.items()}


def _build_lineage_nodes(context, fqns: List[str]) -> Dict[str, Dict[str, Any]]:
    """Build lightweight node metadata for lineage card rendering.

    For each FQN, extracts: name, fqn, type, kind, columns[{name, type}].
    External models (no SQLMesh model found) get an empty columns list.
    Does NOT trigger render_query or column_description to avoid expensive operations.
    """
    from src.api.models.responses import LineageNode as LineageNodeModel

    nodes: Dict[str, Dict[str, Any]] = {}
    for fqn in fqns:
        model = context.get_model(fqn, raise_if_missing=False)
        if model:
            # Extract columns from columns_to_types (lightweight, no render)
            columns = []
            columns_to_types = model.columns_to_types or {}
            for col_name, data_type in columns_to_types.items():
                columns.append({"name": col_name, "type": str(data_type)})

            # Determine model type
            model_type = "sql"
            if hasattr(model, "kind") and model.kind:
                kind_name = model.kind.name if hasattr(model.kind, "name") else str(model.kind)
            else:
                kind_name = ""
            if hasattr(model, "source_type"):
                model_type = str(model.source_type).lower() if model.source_type else "sql"

            nodes[fqn] = LineageNodeModel(
                name=model.name,
                fqn=model.fqn,
                type=model_type,
                kind=kind_name,
                columns=columns,
            ).model_dump()
        else:
            # External model: only has the FQN as name
            short_name = fqn.split(".")[-1] if "." in fqn else fqn
            nodes[fqn] = LineageNodeModel(
                name=short_name,
                fqn=fqn,
                type="external",
                kind="",
                columns=[],
            ).model_dump()

    return nodes


# ---------------------------------------------------------------------------
# Column lineage helpers (ported from SQLMesh web server)
# ---------------------------------------------------------------------------

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


def _create_lineage_adjacency_list(model_name, column_name, context):
    """Create adjacency list with CTE details (matches SQLMesh web server)."""
    from sqlmesh.core.lineage import lineage
    from src.api.models.responses import LineageColumn as LineageColumnModel

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    visited = set()
    visited.add((model_name, column_name))
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
                dependencies = defaultdict(set, graph[node_name][node_column].get("models", {}))
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
                    if isinstance(d.expression, exp.Table) and (table, dep_column) not in visited:
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
    """Merge `extra` column-lineage adjacency into `base` in place.

    Columns present in both are merged at the ``models`` level (union of the
    upstream dependency columns per source model).
    """
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


def _create_models_only_downstream_adjacency_list(model_name, column_name, context):
    """Create a downstream adjacency list with model-level dependencies only.

    Traverses descendant models and records every downstream column that
    depends on the traced column. The output uses the same shape as the
    upstream helper: ``{downstream_model: {downstream_col: {models:
    {source_model: [source_col]}}}}``, so the client can render source→target
    connections without special-casing direction.
    """
    from sqlmesh.core.lineage import column_dependencies
    from src.api.models.responses import LineageColumn as LineageColumnModel

    # Build a parent -> children map (the DAG stores child -> parents).
    children_map: Dict[str, Set[str]] = defaultdict(set)
    for child, parents in context.dag.graph.items():
        for parent in parents:
            children_map[parent].add(child)

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    queue: List[tuple] = [(model_name, column_name)]
    visited: Set[tuple] = {(model_name, column_name)}

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
                # Does this child column depend on (current_model, current_column)?
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


def _create_models_only_lineage_adjacency_list(model_name, column_name, context):
    """Create adjacency list with model-level dependencies only."""
    from sqlmesh.core.lineage import column_dependencies
    from src.api.models.responses import LineageColumn as LineageColumnModel

    graph: Dict[str, Dict[str, dict]] = defaultdict(dict)
    nodes = [(model_name, column_name)]
    visited = set()
    visited.add((model_name, column_name))

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


# Singleton
_lineage_service: Optional[LineageService] = None


def get_lineage_service() -> LineageService:
    global _lineage_service
    if _lineage_service is None:
        _lineage_service = LineageService()
    return _lineage_service
