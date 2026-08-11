"""Semantic search service — the core retrieval API for AI agents.

Provides six methods that let agents progressively discover and understand
business concepts without loading the entire ontology at once:

1. ``search_entities`` — keyword search with scoring
2. ``search_metrics`` — keyword search with scoring
3. ``search_relationships`` — keyword search with scoring
4. ``get_entity_detail`` — full entity info with upstream/downstream links
5. ``get_metric_detail`` — full metric definition
6. ``get_entity_graph`` — BFS relationship subgraph
"""

from __future__ import annotations

from collections import deque
from typing import Optional

import structlog

from src.loader import ModelRegistry
from src.models import (
    AIContext,
    Entity,
    EntityDetail,
    EntityGraph,
    EntityNode,
    EntitySummary,
    GraphEdge,
    Metric,
    MetricDetail,
    MetricSummary,
    Relationship,
    RelationshipSummary,
    RelLink,
    SemanticModel,
)

logger = structlog.get_logger(__name__)

# Scoring weights (per design doc §6.3)
SCORE_EXACT_NAME = 100
SCORE_SUBCONCEPT_EXACT = 90
SCORE_SYNONYM_EXACT = 80
SCORE_NAME_PARTIAL = 50
SCORE_VERBALIZES = 40
SCORE_ENTITY_NAME_IN_REL = 30
SCORE_SYNONYM_PARTIAL = 30
SCORE_SUBCONCEPT_PARTIAL = 40
SCORE_DESCRIPTION = 20


class SemanticSearchService:
    """Agent semantic search toolset — progressive on-demand retrieval.

    The service wraps a :class:`ModelRegistry` and lazily loads full
    :class:`SemanticModel` instances on first access.  All search methods
    return lightweight summary objects; detail methods return richer
    structures with relationship context.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry
        self._loaded: dict[str, SemanticModel] = {}

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load(self, model_name: str) -> SemanticModel:
        """Lazily load and cache a SemanticModel via the registry."""
        if model_name not in self._loaded:
            self._loaded[model_name] = self._registry.get_model(model_name)
        return self._loaded[model_name]

    # ------------------------------------------------------------------
    # Tool 1: search_entities
    # ------------------------------------------------------------------

    def search_entities(
        self, model_name: str, keyword: str, limit: int = 5
    ) -> list[EntitySummary]:
        """Search entities by keyword.

        Matches against entity name, description, ai_context.synonyms, and
        sub-concept names.  Returns results sorted by match score (descending).

        Args:
            model_name: Ontology name (file stem).
            keyword: Search keyword (case-insensitive, supports CJK).
            limit: Maximum number of results to return.

        Returns:
            List of EntitySummary sorted by match_score descending.
        """
        model = self._load(model_name)
        kw = keyword.lower()
        results: list[EntitySummary] = []

        for entity in model.entities.values():
            score = self._entity_match_score(entity, kw)
            if score > 0:
                results.append(EntitySummary(
                    name=entity.name,
                    type=entity.type,
                    description=entity.description,
                    model_ref=entity.model,
                    match_score=score,
                    match_reason=self._entity_match_reason(entity, kw),
                ))

        results.sort(key=lambda r: r.match_score, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Tool 2: search_metrics
    # ------------------------------------------------------------------

    def search_metrics(
        self, model_name: str, keyword: str, limit: int = 5
    ) -> list[MetricSummary]:
        """Search metrics by keyword.

        Matches against metric name, description, and ai_context.synonyms.
        """
        model = self._load(model_name)
        kw = keyword.lower()
        results: list[MetricSummary] = []

        for metric in model.metrics.values():
            score = self._metric_match_score(metric, kw)
            if score > 0:
                results.append(MetricSummary(
                    name=metric.name,
                    description=metric.description,
                    aggregate=metric.aggregate,
                    expression=metric.expression,
                    entity=metric.entity,
                    entities=list(metric.entities),
                    filter=metric.filter,
                    is_derived=metric.is_derived(),
                    is_mapped=metric.is_mapped(),
                    match_score=score,
                ))

        results.sort(key=lambda r: r.match_score, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Tool 3: search_relationships
    # ------------------------------------------------------------------

    def search_relationships(
        self, model_name: str, keyword: str, limit: int = 5
    ) -> list[RelationshipSummary]:
        """Search relationships by keyword.

        Matches against relationship name, verbalizes templates, and the
        entity names involved.
        """
        model = self._load(model_name)
        kw = keyword.lower()
        results: list[RelationshipSummary] = []

        for rel in model.relationships.values():
            score = self._rel_match_score(rel, kw)
            if score > 0:
                results.append(RelationshipSummary(
                    name=rel.name,
                    from_entity=rel.from_entity,
                    to_entity=rel.to_entity,
                    multiplicity=rel.multiplicity,
                    verbalizes=list(rel.verbalizes),
                    match_score=score,
                ))

        results.sort(key=lambda r: r.match_score, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Tool 4: get_entity_detail
    # ------------------------------------------------------------------

    def get_entity_detail(
        self, model_name: str, entity_name: str
    ) -> EntityDetail:
        """Get the full detail of an entity.

        Includes basic definition (key, attributes, foreign keys, sub-concepts),
        derived rules, constraints, and upstream/downstream relationships with
        JOIN conditions and verbalizations.

        Args:
            model_name: Ontology name.
            entity_name: Entity name to look up.

        Returns:
            EntityDetail with all available context.

        Raises:
            KeyError: If the entity does not exist in the model.
        """
        model = self._load(model_name)

        if entity_name not in model.entities:
            raise KeyError(
                f"Entity '{entity_name}' not found in ontology '{model_name}'"
            )

        entity = model.entities[entity_name]

        # Downstream: this entity is the 'from' side
        downstream: list[RelLink] = []
        for rel in model.relationships.values():
            if rel.from_entity == entity_name:
                downstream.append(RelLink(
                    relationship_name=rel.name,
                    direction="out",
                    target_entity=rel.to_entity,
                    join_conditions=[list(pair) for pair in rel.join],
                    multiplicity=rel.multiplicity,
                    verbalizes=list(rel.verbalizes),
                    via_attribute=rel.via_attribute,
                ))

        # Upstream: this entity is the 'to' side (excluding self-refs already
        # captured in downstream when from == to)
        upstream: list[RelLink] = []
        for rel in model.relationships.values():
            if rel.to_entity == entity_name and rel.from_entity != entity_name:
                upstream.append(RelLink(
                    relationship_name=rel.name,
                    direction="in",
                    target_entity=rel.from_entity,
                    join_conditions=[list(pair) for pair in rel.join],
                    multiplicity=rel.multiplicity,
                    verbalizes=list(rel.verbalizes),
                    via_attribute=rel.via_attribute,
                ))

        from src.models import AttrSummary, FKSummary, SubConceptSummary

        return EntityDetail(
            name=entity.name,
            type=entity.type,
            description=entity.description,
            model_ref=entity.model,
            key_mapping=dict(entity.key),
            attributes=[
                AttrSummary(name=a.name, column=a.column, type=a.type)
                for a in entity.attributes
            ],
            foreign_keys=[
                FKSummary(name=fk.name, column=fk.column, ref=fk.references)
                for fk in entity.foreign_keys
            ],
            sub_concepts=[
                SubConceptSummary(
                    name=sc.name,
                    derived_by=sc.derived_by,
                    description=sc.description,
                )
                for sc in entity.sub_concepts.values()
            ],
            extends=list(entity.extends),
            derived_by=list(entity.derived_by),
            identify_by=list(entity.identify_by),
            requires=list(entity.requires),
            downstream_relationships=downstream,
            upstream_relationships=upstream,
            ai_context=entity.ai_context,
        )

    # ------------------------------------------------------------------
    # Tool 5: get_metric_detail
    # ------------------------------------------------------------------

    def get_metric_detail(
        self, model_name: str, metric_name: str
    ) -> MetricDetail:
        """Get the full definition of a metric.

        Args:
            model_name: Ontology name.
            metric_name: Metric name to look up.

        Returns:
            MetricDetail with aggregate, expression, filter, derived formula,
            and AI context.

        Raises:
            KeyError: If the metric does not exist in the model.
        """
        model = self._load(model_name)

        if metric_name not in model.metrics:
            raise KeyError(
                f"Metric '{metric_name}' not found in ontology '{model_name}'"
            )

        metric = model.metrics[metric_name]

        return MetricDetail(
            name=metric.name,
            description=metric.description,
            aggregate=metric.aggregate,
            expression=metric.expression,
            entity=metric.entity,
            entities=list(metric.entities),
            filter=metric.filter,
            is_derived=metric.is_derived(),
            derived_formula=metric.derived.formula if metric.is_derived() else None,
            is_mapped=metric.is_mapped(),
            maps_to=metric.maps_to,
            ai_context=metric.ai_context,
        )

    # ------------------------------------------------------------------
    # Tool 6: get_entity_graph
    # ------------------------------------------------------------------

    def get_entity_graph(
        self, model_name: str, entity_name: str, depth: int = 1
    ) -> EntityGraph:
        """Get a relationship subgraph centred on a specific entity.

        Uses BFS to expand outward from the centre entity up to ``depth``
        hops.  ``depth=1`` returns direct neighbours; ``depth=2`` returns
        neighbours of neighbours.

        Args:
            model_name: Ontology name.
            entity_name: Centre entity name.
            depth: BFS expansion depth (default 1).

        Returns:
            EntityGraph with nodes and edges.

        Raises:
            KeyError: If the entity does not exist in the model.
        """
        model = self._load(model_name)

        if entity_name not in model.entities:
            raise KeyError(
                f"Entity '{entity_name}' not found in ontology '{model_name}'"
            )

        nodes: dict[str, EntityNode] = {
            entity_name: EntityNode(
                name=model.entities[entity_name].name,
                model=model.entities[entity_name].model,
                key=dict(model.entities[entity_name].key),
            )
        }
        edges: list[GraphEdge] = []

        # BFS traversal
        queue: deque[tuple[str, int]] = deque([(entity_name, 0)])
        visited: set[str] = {entity_name}

        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue

            for rel in model.relationships.values():
                neighbour: Optional[str] = None
                if rel.from_entity == current and rel.to_entity not in visited:
                    neighbour = rel.to_entity
                elif rel.to_entity == current and rel.from_entity not in visited:
                    neighbour = rel.from_entity

                if neighbour is None:
                    continue

                visited.add(neighbour)
                ent = model.entities[neighbour]
                nodes[neighbour] = EntityNode(
                    name=ent.name,
                    model=ent.model,
                    key=dict(ent.key),
                )
                edges.append(GraphEdge(
                    from_entity=rel.from_entity,
                    to_entity=rel.to_entity,
                    relationship=rel.name,
                    join_conditions=[list(pair) for pair in rel.join],
                    verbalizes=list(rel.verbalizes),
                ))
                queue.append((neighbour, d + 1))

        return EntityGraph(
            center=entity_name,
            depth=depth,
            nodes=nodes,
            edges=edges,
        )

    # ------------------------------------------------------------------
    # Private scoring helpers
    # ------------------------------------------------------------------

    def _entity_match_score(self, entity: Entity, kw: str) -> int:
        """Calculate match score for an entity against a keyword."""
        score = 0

        # Exact name match
        if entity.name.lower() == kw:
            score += SCORE_EXACT_NAME
        elif kw in entity.name.lower():
            score += SCORE_NAME_PARTIAL

        # Description match
        if entity.description and kw in entity.description.lower():
            score += SCORE_DESCRIPTION

        # Synonym matches
        for syn in _get_synonyms(entity.ai_context):
            syn_lower = syn.lower()
            if syn_lower == kw:
                score += SCORE_SYNONYM_EXACT
            elif kw in syn_lower or syn_lower in kw:
                score += SCORE_SYNONYM_PARTIAL

        # Sub-concept matches
        for sc in entity.sub_concepts.values():
            sc_lower = sc.name.lower()
            if sc_lower == kw:
                score += SCORE_SUBCONCEPT_EXACT
            elif kw in sc_lower:
                score += SCORE_SUBCONCEPT_PARTIAL
            if sc.description and kw in sc.description.lower():
                score += SCORE_DESCRIPTION

        return score

    def _entity_match_reason(self, entity: Entity, kw: str) -> str:
        """Generate a human-readable explanation for why an entity matched."""
        reasons: list[str] = []

        if entity.name.lower() == kw:
            reasons.append("exact name match")
        elif kw in entity.name.lower():
            reasons.append("partial name match")

        for syn in _get_synonyms(entity.ai_context):
            if kw in syn.lower() or syn.lower() in kw:
                reasons.append(f"synonym match: {syn}")

        for sc in entity.sub_concepts.values():
            if kw in sc.name.lower() or sc.name.lower() in kw:
                reasons.append(f"sub-concept match: {sc.name}")

        return "; ".join(reasons) if reasons else "keyword match"

    def _metric_match_score(self, metric: Metric, kw: str) -> int:
        """Calculate match score for a metric against a keyword."""
        score = 0

        if metric.name.lower() == kw:
            score += SCORE_EXACT_NAME
        elif kw in metric.name.lower():
            score += SCORE_NAME_PARTIAL

        if metric.description and kw in metric.description.lower():
            score += SCORE_DESCRIPTION

        for syn in _get_synonyms(metric.ai_context):
            syn_lower = syn.lower()
            if syn_lower == kw:
                score += SCORE_SYNONYM_EXACT
            elif kw in syn_lower or syn_lower in kw:
                score += SCORE_SYNONYM_PARTIAL

        return score

    def _rel_match_score(self, rel: Relationship, kw: str) -> int:
        """Calculate match score for a relationship against a keyword."""
        score = 0

        # Relationship name match
        if rel.name.lower() == kw:
            score += SCORE_EXACT_NAME
        elif kw in rel.name.lower():
            score += SCORE_NAME_PARTIAL

        # Entity name match
        if rel.from_entity.lower() in kw or rel.to_entity.lower() in kw:
            score += SCORE_ENTITY_NAME_IN_REL

        # Verbalizes match
        for v in rel.verbalizes:
            if kw in v.lower():
                score += SCORE_VERBALIZES

        return score


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _get_synonyms(ai_context: Optional[object]) -> list[str]:
    """Extract synonyms from an ai_context field (str or AIContext)."""
    if ai_context is None:
        return []
    if isinstance(ai_context, str):
        return []
    if isinstance(ai_context, AIContext):
        return ai_context.synonyms
    # Fallback for dict-like objects (shouldn't happen with Pydantic)
    if isinstance(ai_context, dict):
        syns = ai_context.get("synonyms", [])
        return syns if isinstance(syns, list) else []
    return []
