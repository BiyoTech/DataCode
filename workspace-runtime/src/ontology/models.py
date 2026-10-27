"""Pydantic v2 data models for the ontology semantic layer.

Domain models (Entity, Relationship, Metric, SemanticModel) mirror the YAML
schema defined in ``doc/semantic-layer-design.md`` §4.  Return-type models
(Summary / Detail / Graph) are used by ``SemanticSearchService`` to provide
structured responses to AI agents.

All models use Pydantic v2 for validation and JSON serialisation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

# ============================================================
# Schema validation error — actionable, client-safe details
# ============================================================

def _loc_to_dotted_path(loc: tuple[Any, ...]) -> str:
    """Render a Pydantic error location tuple as a readable dotted path.

    Integer indices are rendered as ``[i]`` so list positions stay obvious,
    e.g. ``("entities", "Order", "attributes", 3, "type")`` becomes
    ``entities.Order.attributes[3].type``.
    """
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts)


def _format_pydantic_errors(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Convert a Pydantic ValidationError into a client-safe error list.

    Each item exposes the field location, the human-readable message, the
    rejected input value, and the raw error type so callers (API/CLI) can
    surface actionable fix guidance instead of a generic failure message.
    """
    formatted: list[dict[str, Any]] = []
    for err in exc.errors():
        formatted.append(
            {
                "location": _loc_to_dotted_path(err.get("loc", ())),
                "message": err.get("msg", ""),
                "input": err.get("input"),
                "type": err.get("type", ""),
            }
        )
    return formatted


class OntologySchemaError(ValueError):
    """Raised when an ontology YAML fails Pydantic schema validation.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working, but additionally carries a structured, client-safe list of
    field-level errors. This lets the API and CLI report exactly which fields
    are wrong and how to fix them, rather than a generic "invalid" message.
    """

    def __init__(self, ontology_name: str, errors: list[dict[str, Any]]) -> None:
        self.ontology_name = ontology_name
        self.errors = errors
        super().__init__(
            f"Ontology '{ontology_name}' failed schema validation "
            f"with {len(errors)} error(s)"
        )


# ============================================================
# AI Context — structured prompts attachable at every level
# ============================================================

class AIContext(BaseModel):
    """Structured AI context with synonyms, instructions, and examples."""

    model_config = ConfigDict(extra="forbid")

    instructions: Optional[str] = None
    synonyms: list[str] = []
    examples: list[str] = []


# ============================================================
# Domain models — Entity and its sub-structures
# ============================================================

class Attribute(BaseModel):
    """A dimension or property of an entity, mapped to a physical column."""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str
    type: Literal["categorical", "time", "numeric", "text"] = "categorical"
    granularity: Optional[str] = None
    description: Optional[str] = None
    ai_context: Optional[Union[str, AIContext]] = None


class ForeignKey(BaseModel):
    """A foreign key referencing another entity's attribute."""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str
    references: str  # e.g. "Customer.customer_id"


class SubConcept(BaseModel):
    """A derived sub-concept that inherits from its parent entity."""

    model_config = ConfigDict(extra="forbid")

    name: str
    derived_by: Union[str, list[str]]
    description: Optional[str] = None
    ai_context: Optional[Union[str, AIContext]] = None


class Entity(BaseModel):
    """A business concept mapped to a physical SQLMesh model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    type: Literal["EntityType", "ValueType"] = "EntityType"
    model: Optional[str] = None  # SQLMesh fully-qualified model name
    key: dict[str, str] = {}  # concept attribute -> physical column
    attributes: list[Attribute] = []
    foreign_keys: list[ForeignKey] = []
    sub_concepts: dict[str, SubConcept] = {}
    extends: list[str] = []
    derived_by: list[str] = []
    identify_by: list[str] = []
    requires: list[str] = []
    ai_context: Optional[Union[str, AIContext]] = None

    @model_validator(mode="before")
    @classmethod
    def _convert_sub_concepts_to_dict(cls, data: Any) -> Any:
        """Convert the ``sub_concepts`` YAML list into a name-keyed dict."""
        if isinstance(data, dict):
            items = data.get("sub_concepts", [])
            if isinstance(items, list):
                data["sub_concepts"] = {
                    item["name"]: item for item in items
                }
        return data


# ============================================================
# Domain models — Relationship
# ============================================================

class Relationship(BaseModel):
    """A semantic relationship between entities with JOIN conditions.

    The YAML keys ``from`` and ``to`` are Python reserved words, so they are
    mapped to ``from_entity`` / ``to_entity`` via field aliases.  The YAML
    files remain unchanged.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    from_entity: str = Field(alias="from")
    to_entity: str = Field(alias="to")
    join: list[list[str]] = []  # [[from_attr, to_attr], ...]
    via_attribute: Optional[str] = None
    multiplicity: Optional[Literal["ManyToOne", "OneToOne"]] = None
    description: Optional[str] = None
    verbalizes: list[str] = []
    derived_by: list[str] = []
    requires: list[str] = []
    ai_context: Optional[Union[str, AIContext]] = None


# ============================================================
# Domain models — Metric
# ============================================================

class DerivedMetric(BaseModel):
    """Container for a derived metric's formula."""

    model_config = ConfigDict(extra="forbid")

    formula: str


class Metric(BaseModel):
    """A business metric: base, filtered, derived, or mapped to SQLMesh METRIC."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    aggregate: Optional[
        Literal["COUNT", "SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"]
    ] = None
    expression: Optional[str] = None  # e.g. "Order.order_id"
    entity: Optional[str] = None
    entities: list[str] = []
    filter: Optional[str] = None
    derived: Optional[DerivedMetric] = None
    maps_to: Optional[str] = None  # references an existing SQLMesh METRIC
    ai_context: Optional[Union[str, AIContext]] = None

    def is_derived(self) -> bool:
        """Return True if this metric is derived from other metrics."""
        return self.derived is not None

    def is_mapped(self) -> bool:
        """Return True if this metric maps to an existing SQLMesh METRIC."""
        return self.maps_to is not None


# ============================================================
# Domain models — SemanticModel (top-level)
# ============================================================

class SemanticModel(BaseModel):
    """Top-level semantic model: one YAML file = one business domain.

    The ``name`` field is **not** read from YAML — it is injected by
    :meth:`from_yaml` from the file name (without ``.yaml`` extension).
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    name: str  # injected from filename, not present in YAML
    description: Optional[str] = None
    # SQLMesh gateway this ontology is built on. All entity-backing models must
    # belong to this single gateway (base models without a gateway are shared
    # and remain compatible). Optional: ontologies referencing only base models
    # may omit it. See CrossRefValidator._validate_gateway_consistency.
    gateway: Optional[str] = None
    ai_context: Optional[Union[str, AIContext]] = None
    entities: dict[str, Entity] = {}
    relationships: dict[str, Relationship] = {}
    metrics: dict[str, Metric] = {}

    @model_validator(mode="before")
    @classmethod
    def _convert_lists_to_dicts(cls, data: Any) -> Any:
        """Convert YAML lists for entities / relationships / metrics into
        name-keyed dicts for O(1) lookup."""
        if isinstance(data, dict):
            for key in ("entities", "relationships", "metrics"):
                items = data.get(key, [])
                if isinstance(items, list):
                    data[key] = {
                        item["name"]: item for item in items
                    }
        return data

    @classmethod
    def from_yaml(cls, path: Path, name: str) -> "SemanticModel":
        """Parse a YAML file and build a validated SemanticModel.

        The ``name`` parameter is derived from the file name (without ``.yaml``)
        by the caller — it is **not** read from the YAML content.

        Uses ``yaml.safe_load`` to prevent arbitrary code execution (RCE).
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(
                f"Ontology YAML must be a mapping at the top level, "
                f"got {type(raw).__name__} in {path}"
            )
        raw["name"] = name
        try:
            return cls.model_validate(raw)
        except PydanticValidationError as exc:
            # Re-raise with structured, actionable field errors so the API/CLI
            # can tell the user exactly which fields to fix.
            raise OntologySchemaError(name, _format_pydantic_errors(exc)) from exc


# ============================================================
# Validation error model
# ============================================================

class ValidationError(BaseModel):
    """A single cross-reference validation error."""

    model_config = ConfigDict(extra="forbid")

    path: str  # dotted path to the offending field, e.g. "relationships[0].from"
    message: str
    severity: Literal["error", "warning"] = "error"


# ============================================================
# Return types — Summary models (for search_* methods)
# ============================================================

class ModelSummary(BaseModel):
    """Lightweight summary of a semantic model for listing."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    keywords: list[str] = []


class EntitySummary(BaseModel):
    """Search result summary for an entity."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    description: Optional[str] = None
    model_ref: Optional[str] = None
    match_score: int = 0
    match_reason: str = ""


class MetricSummary(BaseModel):
    """Search result summary for a metric."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    aggregate: Optional[str] = None
    expression: Optional[str] = None
    entity: Optional[str] = None
    entities: list[str] = []
    filter: Optional[str] = None
    is_derived: bool = False
    is_mapped: bool = False
    match_score: int = 0


class RelationshipSummary(BaseModel):
    """Search result summary for a relationship."""

    model_config = ConfigDict(extra="forbid")

    name: str
    from_entity: str
    to_entity: str
    multiplicity: Optional[str] = None
    verbalizes: list[str] = []
    match_score: int = 0


# ============================================================
# Return types — Detail models (for get_*_detail methods)
# ============================================================

class AttrSummary(BaseModel):
    """Summary of an entity attribute."""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str
    type: Optional[str] = None


class FKSummary(BaseModel):
    """Summary of a foreign key."""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str
    ref: str


class SubConceptSummary(BaseModel):
    """Summary of a sub-concept."""

    model_config = ConfigDict(extra="forbid")

    name: str
    derived_by: Union[str, list[str]]
    description: Optional[str] = None


class RelLink(BaseModel):
    """A relationship link in an entity's upstream/downstream list."""

    model_config = ConfigDict(extra="forbid")

    relationship_name: str
    direction: Literal["in", "out"]
    target_entity: str
    join_conditions: list[list[str]] = []
    multiplicity: Optional[str] = None
    verbalizes: list[str] = []
    via_attribute: Optional[str] = None


class EntityDetail(BaseModel):
    """Full detail of an entity including upstream/downstream relationships."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    description: Optional[str] = None
    model_ref: Optional[str] = None
    key_mapping: dict[str, str] = {}
    attributes: list[AttrSummary] = []
    foreign_keys: list[FKSummary] = []
    sub_concepts: list[SubConceptSummary] = []
    extends: list[str] = []
    derived_by: list[str] = []
    identify_by: list[str] = []
    requires: list[str] = []
    downstream_relationships: list[RelLink] = []
    upstream_relationships: list[RelLink] = []
    ai_context: Optional[Union[str, AIContext]] = None


class MetricDetail(BaseModel):
    """Full detail of a metric."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    aggregate: Optional[str] = None
    expression: Optional[str] = None
    entity: Optional[str] = None
    entities: list[str] = []
    filter: Optional[str] = None
    is_derived: bool = False
    derived_formula: Optional[str] = None
    is_mapped: bool = False
    maps_to: Optional[str] = None
    ai_context: Optional[Union[str, AIContext]] = None


# ============================================================
# Return types — Graph models (for get_entity_graph)
# ============================================================

class EntityNode(BaseModel):
    """A node in the entity relationship graph."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: Optional[str] = None
    key: dict[str, str] = {}


class GraphEdge(BaseModel):
    """An edge in the entity relationship graph."""

    model_config = ConfigDict(extra="forbid")

    from_entity: str
    to_entity: str
    relationship: str
    join_conditions: list[list[str]] = []
    verbalizes: list[str] = []


class EntityGraph(BaseModel):
    """A subgraph centred on a specific entity."""

    model_config = ConfigDict(extra="forbid")

    center: str
    depth: int = 1
    nodes: dict[str, EntityNode] = {}
    edges: list[GraphEdge] = []
