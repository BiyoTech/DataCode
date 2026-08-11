"""Ontology Service - Semantic layer for AI-native data exploration.

Provides structured semantic retrieval of business concepts (entities,
relationships, metrics) defined in per-workspace ontology YAML files.
"""

from src.cross_ref_validator import CrossRefValidator, ValidationError
from src.loader import ModelRegistry
from src.models import (
    AIContext,
    Attribute,
    Entity,
    EntityDetail,
    EntityGraph,
    EntityNode,
    EntitySummary,
    FKSummary,
    GraphEdge,
    Metric,
    MetricDetail,
    MetricSummary,
    ModelSummary,
    Relationship,
    RelationshipSummary,
    RelLink,
    SemanticModel,
    SubConcept,
    SubConceptSummary,
)
from src.semantic_search import SemanticSearchService

__all__ = [
    "AIContext",
    "Attribute",
    "CrossRefValidator",
    "Entity",
    "EntityDetail",
    "EntityGraph",
    "EntityNode",
    "EntitySummary",
    "FKSummary",
    "GraphEdge",
    "Metric",
    "MetricDetail",
    "MetricSummary",
    "ModelRegistry",
    "ModelSummary",
    "Relationship",
    "RelationshipSummary",
    "RelLink",
    "SemanticModel",
    "SemanticSearchService",
    "SubConcept",
    "SubConceptSummary",
    "ValidationError",
]
