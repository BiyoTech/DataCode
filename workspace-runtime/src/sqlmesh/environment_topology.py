"""Environment-scoped topology helpers.

SQLMesh's in-memory ``context.dag`` / ``context.models`` reflect the *local*
project definition (what is on disk), which is effectively the not-yet-planned
"dev definition" view. A promoted virtual environment (e.g. ``prod`` / ``dev``)
instead stores a concrete set of snapshots in state. Those snapshots capture the
exact model definitions and their parent edges as they were when the environment
was last applied.

This module bridges that gap: given a context and an environment name, it reads
the environment's snapshots from the state reader and reconstructs the same
``(graph, nodes)`` shapes used elsewhere in the service, so lineage and model
listing can be served per environment without changing their downstream
formatting.

Design notes:
- Keyed by snapshot ``name`` (the model FQN) so the output is drop-in compatible
  with the local-definition topology (same FQN keys, same ``nodes`` metadata).
- ``parents`` on a snapshot are ``SnapshotId`` objects; we map them back to FQNs
  via the environment's own snapshot set so edges only reference nodes that
  actually exist in the environment.
- Fails soft on a per-node basis: a snapshot we cannot introspect still appears
  as a node (with minimal metadata) rather than dropping it from the graph.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Environment names that mean "use the local project definition" rather than a
# promoted environment's snapshots. Empty / unset falls through to local too.
_LOCAL_ENVIRONMENT_ALIASES = {"", "local"}


def is_local_environment(context, environment: Optional[str]) -> bool:
    """Whether ``environment`` should be served from the local definition view.

    Returns True when the environment is empty, an explicit local alias, or a
    promoted environment that does not (yet) exist in state. In all those cases
    callers should fall back to ``context.dag`` / ``context.models``.
    """
    name = (environment or "").strip().lower()
    if name in _LOCAL_ENVIRONMENT_ALIASES:
        return True

    # A promoted environment must exist in state to be served from snapshots.
    # If it was never planned/applied, fall back to the local definition so the
    # UI still shows the (development) topology instead of an empty graph.
    try:
        env = context.state_reader.get_environment(name)
    except Exception as e:  # never fail the request on a state read hiccup
        logger.warning("environment_lookup_failed", environment=name, error=str(e))
        return True
    return env is None


def _load_environment_snapshots(context, environment: str) -> Dict[Any, Any]:
    """Load the full ``Snapshot`` objects for a promoted environment.

    Returns a mapping of ``SnapshotId -> Snapshot``. The environment stores only
    lightweight ``SnapshotTableInfo`` records, so we hydrate the full snapshots
    (which carry ``parents`` and the model definition) from the state reader.
    """
    env = context.state_reader.get_environment(environment)
    if env is None or not env.snapshots:
        return {}

    snapshot_ids = [s.snapshot_id for s in env.snapshots]
    return context.state_reader.get_snapshots(snapshot_ids)


def _node_metadata_from_snapshot(snapshot) -> Dict[str, Any]:
    """Build lightweight node metadata (name, fqn, type, kind, columns).

    Mirrors ``lineage_service._build_lineage_nodes`` so environment-scoped nodes
    render identically to local-definition ones. Reads only cheap attributes off
    the snapshot's model (no query rendering).
    """
    fqn = snapshot.name
    model = getattr(snapshot, "model", None)

    # Non-model snapshots (audits, seeds handled as models still expose a model)
    # or snapshots without an attached node: expose a minimal external-style node.
    if model is None:
        short_name = fqn.split(".")[-1].strip('"') if "." in fqn else fqn
        return {
            "name": short_name,
            "fqn": fqn,
            "type": "external",
            "kind": "",
            "columns": [],
        }

    columns: List[Dict[str, str]] = []
    for col_name, data_type in (getattr(model, "columns_to_types", None) or {}).items():
        columns.append({"name": col_name, "type": str(data_type)})

    kind = getattr(model, "kind", None)
    kind_name = getattr(kind, "name", "") if kind else ""
    if not isinstance(kind_name, str):
        kind_name = str(kind_name)

    source_type = getattr(model, "source_type", None)
    model_type = str(source_type).lower() if source_type else "sql"

    return {
        "name": model.name,
        "fqn": model.fqn,
        "type": model_type,
        "kind": kind_name,
        "columns": columns,
    }


def build_environment_graph_with_nodes(
    context, environment: str
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Build ``(graph, nodes)`` for a promoted environment from its snapshots.

    - graph: ``{fqn: [upstream_dep_fqn, ...]}`` for every model snapshot in the
      environment (edges restricted to nodes present in the environment).
    - nodes: ``{fqn: {name, fqn, type, kind, columns}}`` for card rendering and
      display-name resolution.
    """
    snapshots_by_id = _load_environment_snapshots(context, environment)

    # Map each SnapshotId to its FQN so parent edges can be resolved to names.
    id_to_fqn: Dict[Any, str] = {
        sid: snap.name for sid, snap in snapshots_by_id.items()
    }
    present_fqns: Set[str] = set(id_to_fqn.values())

    graph: Dict[str, List[str]] = {}
    nodes: Dict[str, Dict[str, Any]] = {}

    for sid, snapshot in snapshots_by_id.items():
        fqn = snapshot.name

        # Only model snapshots participate in the lineage graph. Skip audits and
        # other non-model node types so the DAG matches the model listing.
        if not _is_model_snapshot(snapshot):
            continue

        deps: List[str] = []
        for parent_id in getattr(snapshot, "parents", []) or []:
            parent_fqn = id_to_fqn.get(parent_id)
            if parent_fqn and parent_fqn in present_fqns:
                deps.append(parent_fqn)

        graph[fqn] = sorted(set(deps))
        nodes[fqn] = _node_metadata_from_snapshot(snapshot)

    logger.info(
        "environment_topology_built",
        environment=environment,
        models=len(graph),
    )
    return graph, nodes


def build_environment_model_items(context, environment: str) -> List[Dict[str, Any]]:
    """Build model-list items for a promoted environment from its snapshots.

    Reuses ``format.format_model_list_item`` for each snapshot's model so the
    output is identical in shape to the local-definition listing.
    """
    from src.sqlmesh.format import format_model_list_item

    snapshots_by_id = _load_environment_snapshots(context, environment)

    items: List[Dict[str, Any]] = []
    for snapshot in snapshots_by_id.values():
        if not _is_model_snapshot(snapshot):
            continue
        model = getattr(snapshot, "model", None)
        if model is None:
            continue
        try:
            items.append(format_model_list_item(model))
        except Exception as e:  # never drop the whole list on one bad snapshot
            logger.warning(
                "environment_model_item_failed",
                environment=environment,
                model=getattr(model, "name", "?"),
                error=str(e),
            )

    return items


def resolve_model_fqn_in_environment(
    context, nodes: Dict[str, Dict[str, Any]], model_name: str
) -> Optional[str]:
    """Resolve a caller-supplied model identifier to an FQN within ``nodes``.

    Accepts either an FQN (direct key hit) or a display name (``schema.table``)
    and returns the matching FQN, or None when the model is absent from the
    environment. Comparison is tolerant of surrounding quotes and case.
    """
    if model_name in nodes:
        return model_name

    def _norm(value: str) -> str:
        return (value or "").replace('"', "").lower()

    target = _norm(model_name)
    for fqn, meta in nodes.items():
        if _norm(fqn) == target or _norm(meta.get("name", "")) == target:
            return fqn
    return None


def _is_model_snapshot(snapshot) -> bool:
    """Whether a snapshot represents a SQLMesh model (vs. audit/other node).

    ``is_model`` is the canonical flag on modern SQLMesh snapshots; fall back to
    the presence of an attached ``model`` for older versions.
    """
    is_model = getattr(snapshot, "is_model", None)
    if is_model is not None:
        return bool(is_model)
    return getattr(snapshot, "model", None) is not None
