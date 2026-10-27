"""Cross-reference validator for semantic models.

Validates that all references between entities, relationships, and metrics
are consistent.  Collects **all** errors before returning so users can fix
them in one pass.
"""

from __future__ import annotations

import re
from typing import Optional

import structlog

from src.ontology.models import Entity, SemanticModel, ValidationError

logger = structlog.get_logger(__name__)

# Built-in / primitive value types an entity may ``extends`` without them being
# declared as entities in the same model (e.g. ``Amount extends [Decimal]``).
PRIMITIVE_VALUE_TYPES: frozenset[str] = frozenset({
    "String", "Text", "Char", "Varchar",
    "Integer", "Int", "BigInt", "SmallInt",
    "Decimal", "Numeric", "Number", "Float", "Double", "Real",
    "Boolean", "Bool",
    "Date", "DateTime", "Timestamp", "Time",
    "UUID", "JSON", "Binary", "Bytes",
})


class CrossRefValidator:
    """Validates cross-references within a SemanticModel.

    All public methods are stateless — they operate purely on the given model.

    Model-internal checks (entities / relationships / metrics referencing each
    other) run unconditionally. SQLMesh existence checks are opt-in: pass the
    known SQLMesh model / metric identifier sets to enable them. When those
    sets are ``None`` the corresponding checks are skipped, keeping the
    validator usable without any SQLMesh access.
    """

    @staticmethod
    def validate(
        model: SemanticModel,
        sqlmesh_models: Optional[set[str]] = None,
        sqlmesh_metrics: Optional[set[str]] = None,
        sqlmesh_model_gateways: Optional[dict[str, str]] = None,
    ) -> list[ValidationError]:
        """Run all cross-reference checks and collect every error found.

        Args:
            model: The SemanticModel to validate.
            sqlmesh_models: Known SQLMesh model identifiers (name and/or fqn).
                When provided, ``Entity.model`` existence is validated.
            sqlmesh_metrics: Known SQLMesh METRIC names. When provided,
                ``Metric.maps_to`` existence is validated.
            sqlmesh_model_gateways: Map of SQLMesh model identifier -> gateway
                for gateway-bound models. When provided, gateway consistency is
                validated (single-gateway rule + declared-gateway match).

        Returns:
            A list of ValidationError objects (empty if the model is valid).
        """
        errors: list[ValidationError] = []

        # --- Model-internal cross-reference checks (no external deps) ---
        CrossRefValidator._validate_relationships(model, errors)
        CrossRefValidator._validate_foreign_keys(model, errors)
        CrossRefValidator._validate_metrics(model, errors)
        CrossRefValidator._validate_entity_inheritance(model, errors)

        # --- SQLMesh cross-service existence checks (opt-in) ---
        if sqlmesh_models is not None:
            CrossRefValidator._validate_entity_models(model, sqlmesh_models, errors)
        if sqlmesh_metrics is not None:
            CrossRefValidator._validate_metric_maps_to(model, sqlmesh_metrics, errors)
        if sqlmesh_model_gateways is not None:
            CrossRefValidator._validate_gateway_consistency(
                model, sqlmesh_model_gateways, errors
            )

        if errors:
            logger.warning(
                "Cross-reference validation found errors",
                ontology=model.name,
                error_count=len(errors),
            )
        else:
            logger.info(
                "Cross-reference validation passed",
                ontology=model.name,
            )

        return errors

    # ------------------------------------------------------------------
    # Relationship validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_relationships(
        model: SemanticModel, errors: list[ValidationError]
    ) -> None:
        """Validate that relationship.from and relationship.to reference
        existing entities, and that JOIN attributes exist on both sides."""
        for rel_name, rel in model.relationships.items():
            # Check from entity exists
            if rel.from_entity not in model.entities:
                errors.append(ValidationError(
                    path=f"relationships[{rel_name}].from",
                    message=(
                        f"Relationship '{rel_name}' references unknown "
                        f"entity '{rel.from_entity}' in 'from'"
                    ),
                ))
            # Check to entity exists
            if rel.to_entity not in model.entities:
                errors.append(ValidationError(
                    path=f"relationships[{rel_name}].to",
                    message=(
                        f"Relationship '{rel_name}' references unknown "
                        f"entity '{rel.to_entity}' in 'to'"
                    ),
                ))

            # Skip join validation if entities don't exist
            if rel.from_entity not in model.entities or rel.to_entity not in model.entities:
                continue

            from_entity = model.entities[rel.from_entity]
            to_entity = model.entities[rel.to_entity]

            for i, pair in enumerate(rel.join):
                if len(pair) != 2:
                    errors.append(ValidationError(
                        path=f"relationships[{rel_name}].join[{i}]",
                        message=(
                            f"JOIN pair must have exactly 2 elements, "
                            f"got {len(pair)}: {pair}"
                        ),
                    ))
                    continue

                from_attr, to_attr = pair[0], pair[1]
                if not CrossRefValidator._entity_has_attribute(from_entity, from_attr):
                    errors.append(ValidationError(
                        path=f"relationships[{rel_name}].join[{i}][0]",
                        message=(
                            f"JOIN references unknown attribute '{from_attr}' "
                            f"on entity '{rel.from_entity}'"
                        ),
                    ))
                if not CrossRefValidator._entity_has_attribute(to_entity, to_attr):
                    errors.append(ValidationError(
                        path=f"relationships[{rel_name}].join[{i}][1]",
                        message=(
                            f"JOIN references unknown attribute '{to_attr}' "
                            f"on entity '{rel.to_entity}'"
                        ),
                    ))

    # ------------------------------------------------------------------
    # Foreign key validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_foreign_keys(
        model: SemanticModel, errors: list[ValidationError]
    ) -> None:
        """Validate that foreign_key.references points to an existing
        Concept.attribute."""
        for ent_name, entity in model.entities.items():
            for fk in entity.foreign_keys:
                # Parse "Concept.attribute" format
                parts = fk.references.split(".")
                if len(parts) != 2:
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].foreign_keys[{fk.name}]",
                        message=(
                            f"Foreign key reference must be "
                            f"'Concept.attribute', got '{fk.references}'"
                        ),
                    ))
                    continue

                ref_entity_name, ref_attr_name = parts[0], parts[1]

                if ref_entity_name not in model.entities:
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].foreign_keys[{fk.name}]",
                        message=(
                            f"Foreign key references unknown entity "
                            f"'{ref_entity_name}'"
                        ),
                    ))
                    continue

                ref_entity = model.entities[ref_entity_name]
                if not CrossRefValidator._entity_has_attribute(ref_entity, ref_attr_name):
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].foreign_keys[{fk.name}]",
                        message=(
                            f"Foreign key references unknown attribute "
                            f"'{ref_attr_name}' on entity '{ref_entity_name}'"
                        ),
                    ))

    # ------------------------------------------------------------------
    # Metric validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_metrics(
        model: SemanticModel, errors: list[ValidationError]
    ) -> None:
        """Validate metric.entity, metric.entities, metric.expression, and
        metric.derived.formula references."""
        for metric_name, metric in model.metrics.items():
            # Check entity reference (single-entity metrics)
            if metric.entity is not None and metric.entity not in model.entities:
                errors.append(ValidationError(
                    path=f"metrics[{metric_name}].entity",
                    message=(
                        f"Metric references unknown entity '{metric.entity}'"
                    ),
                ))

            # Check entities list (multi-entity metrics)
            for i, ent in enumerate(metric.entities):
                if ent not in model.entities:
                    errors.append(ValidationError(
                        path=f"metrics[{metric_name}].entities[{i}]",
                        message=(
                            f"Metric references unknown entity '{ent}'"
                        ),
                    ))

            # Check expression references (Concept.attribute format)
            if metric.expression:
                ref_errors = CrossRefValidator._validate_concept_attr_ref(
                    metric.expression, model, f"metrics[{metric_name}].expression"
                )
                errors.extend(ref_errors)

            # Check filter references (Concept.attribute format), e.g.
            # "Customer.status = 'ACTIVE'".
            if metric.filter:
                ref_errors = CrossRefValidator._validate_concept_attr_ref(
                    metric.filter, model, f"metrics[{metric_name}].filter"
                )
                errors.extend(ref_errors)

            # Check derived formula references other metric names
            if metric.is_derived() and metric.derived:
                referenced_metrics = CrossRefValidator._extract_metric_names(
                    metric.derived.formula, model
                )
                for ref_name in referenced_metrics:
                    if ref_name not in model.metrics:
                        errors.append(ValidationError(
                            path=f"metrics[{metric_name}].derived.formula",
                            message=(
                                f"Derived formula references unknown "
                                f"metric '{ref_name}'"
                            ),
                        ))

            # Note: maps_to points to an external SQLMesh METRIC. It is
            # validated separately by ``_validate_metric_maps_to`` only when
            # the known SQLMesh metric set is provided to ``validate``.

    # ------------------------------------------------------------------
    # Entity inheritance / derivation validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_entity_inheritance(
        model: SemanticModel, errors: list[ValidationError]
    ) -> None:
        """Validate ``extends``, ``identify_by`` and path-expression fields.

        - ``extends``: each target must be either another entity in the model
          or a known primitive value type (e.g. ``Decimal``).
        - ``identify_by``: each target must be an existing relationship name.
        - ``derived_by`` / ``requires`` (on entities, sub-concepts and
          relationships): the root concept of every ``Entity.path`` expression
          must be a known entity. Attribute/relationship path segments are not
          checked here, since these fields use OSI path syntax (e.g.
          ``EXISTS(Customer.places.Order)``) rather than plain attribute refs.
        """
        for ent_name, entity in model.entities.items():
            # extends -> entity or primitive value type
            for i, parent in enumerate(entity.extends):
                if parent not in model.entities and parent not in PRIMITIVE_VALUE_TYPES:
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].extends[{i}]",
                        message=(
                            f"'extends' references unknown entity or value "
                            f"type '{parent}'"
                        ),
                    ))

            # identify_by -> relationship names
            for i, rel_name in enumerate(entity.identify_by):
                if rel_name not in model.relationships:
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].identify_by[{i}]",
                        message=(
                            f"'identify_by' references unknown relationship "
                            f"'{rel_name}'"
                        ),
                    ))

            # entity-level derived_by / requires path expressions
            for i, expr in enumerate(entity.derived_by):
                errors.extend(CrossRefValidator._validate_path_expr_roots(
                    expr, model, f"entities[{ent_name}].derived_by[{i}]"
                ))
            for i, expr in enumerate(entity.requires):
                errors.extend(CrossRefValidator._validate_path_expr_roots(
                    expr, model, f"entities[{ent_name}].requires[{i}]"
                ))

            # sub-concept derived_by path expressions
            for sc_name, sub in entity.sub_concepts.items():
                formulas = (
                    sub.derived_by
                    if isinstance(sub.derived_by, list)
                    else [sub.derived_by]
                )
                for i, expr in enumerate(formulas):
                    errors.extend(CrossRefValidator._validate_path_expr_roots(
                        expr,
                        model,
                        f"entities[{ent_name}].sub_concepts[{sc_name}].derived_by[{i}]",
                    ))

        # relationship-level derived_by / requires path expressions
        for rel_name, rel in model.relationships.items():
            for i, expr in enumerate(rel.derived_by):
                errors.extend(CrossRefValidator._validate_path_expr_roots(
                    expr, model, f"relationships[{rel_name}].derived_by[{i}]"
                ))
            for i, expr in enumerate(rel.requires):
                errors.extend(CrossRefValidator._validate_path_expr_roots(
                    expr, model, f"relationships[{rel_name}].requires[{i}]"
                ))

    # ------------------------------------------------------------------
    # SQLMesh cross-service existence validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_entity_models(
        model: SemanticModel,
        sqlmesh_models: set[str],
        errors: list[ValidationError],
    ) -> None:
        """Validate that each ``Entity.model`` maps to an existing SQLMesh model."""
        for ent_name, entity in model.entities.items():
            if not entity.model:
                continue
            if not CrossRefValidator._model_id_matches(entity.model, sqlmesh_models):
                errors.append(ValidationError(
                    path=f"entities[{ent_name}].model",
                    message=(
                        f"Entity '{ent_name}' maps to SQLMesh model "
                        f"'{entity.model}', which does not exist in the workspace"
                    ),
                ))

    @staticmethod
    def _validate_metric_maps_to(
        model: SemanticModel,
        sqlmesh_metrics: set[str],
        errors: list[ValidationError],
    ) -> None:
        """Validate that each ``Metric.maps_to`` references an existing SQLMesh METRIC."""
        normalized = {m.strip().lower() for m in sqlmesh_metrics}
        for metric_name, metric in model.metrics.items():
            if not metric.maps_to:
                continue
            if metric.maps_to.strip().lower() not in normalized:
                errors.append(ValidationError(
                    path=f"metrics[{metric_name}].maps_to",
                    message=(
                        f"Metric '{metric_name}' maps to SQLMesh METRIC "
                        f"'{metric.maps_to}', which does not exist in the workspace"
                    ),
                ))

    # ------------------------------------------------------------------
    # Gateway consistency validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_gateway_consistency(
        model: SemanticModel,
        model_gateways: dict[str, str],
        errors: list[ValidationError],
    ) -> None:
        """Enforce the single-gateway rule and the declared-gateway match.

        Rules:
        - An ontology may reference models from at most ONE gateway. Base
          models (no gateway) are shared and impose no constraint.
        - When the ontology declares a top-level ``gateway``, every
          gateway-bound entity model must belong to that same gateway.
        - When gateway-bound models are referenced but no ``gateway`` is
          declared, the ontology must declare one so its binding is explicit.

        Multiple ontologies MAY share the same gateway — this is a per-ontology
        check, not a cross-ontology uniqueness constraint.
        """
        declared = model.gateway

        # Resolve each entity's backing-model gateway (None = base/unknown).
        entity_gateways: dict[str, Optional[str]] = {}
        for ent_name, entity in model.entities.items():
            if not entity.model:
                continue
            entity_gateways[ent_name] = CrossRefValidator._resolve_model_gateway(
                entity.model, model_gateways
            )

        distinct = sorted({gw for gw in entity_gateways.values() if gw})

        # Rule 1: no mixing of models from multiple gateways.
        if len(distinct) > 1:
            offenders = ", ".join(
                f"{name} -> {gw}"
                for name, gw in sorted(entity_gateways.items())
                if gw
            )
            errors.append(ValidationError(
                path="gateway",
                message=(
                    f"Ontology references models from multiple gateways "
                    f"{distinct}; an ontology may only use models from a single "
                    f"gateway. Offending entities: {offenders}"
                ),
            ))

        # Rule 2 / 3: declared-gateway consistency.
        if declared:
            for ent_name, gw in entity_gateways.items():
                if gw is not None and gw != declared:
                    errors.append(ValidationError(
                        path=f"entities[{ent_name}].model",
                        message=(
                            f"Entity '{ent_name}' maps to a model in gateway "
                            f"'{gw}', which differs from the ontology's declared "
                            f"gateway '{declared}'"
                        ),
                    ))
        elif distinct:
            suggestion = distinct[0] if len(distinct) == 1 else "<gateway>"
            errors.append(ValidationError(
                path="gateway",
                message=(
                    f"Ontology references gateway-bound models {distinct} but "
                    f"does not declare a top-level 'gateway' field. Add "
                    f"'gateway: {suggestion}' to make the binding explicit"
                ),
            ))

    @staticmethod
    def _resolve_model_gateway(
        target: str, model_gateways: dict[str, str]
    ) -> Optional[str]:
        """Resolve a semantic-layer model reference to its gateway.

        Uses the same dialect-tolerant matching as ``_model_id_matches``
        (quote-stripped, case-folded, dotted-suffix aware). Returns ``None``
        for base models (not present in the gateway map).
        """
        t = CrossRefValidator._normalize_model_id(target)
        for identifier, gateway in model_gateways.items():
            c = CrossRefValidator._normalize_model_id(identifier)
            if t == c or c.endswith("." + t):
                return gateway
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_path_expr_roots(
        expression: str,
        model: SemanticModel,
        path_prefix: str,
    ) -> list[ValidationError]:
        """Validate that the root concept of each ``Entity.path`` is a known entity.

        Used for OSI path expressions (``derived_by`` / ``requires``) such as
        ``EXISTS(Customer.places.Order)``. Only capitalized identifiers that are
        immediately followed by a ``.`` are treated as entity roots; function
        calls like ``EXISTS(`` (no trailing dot) are ignored, and lowercase path
        segments (relationships/attributes) are intentionally not checked here.
        """
        errors: list[ValidationError] = []
        seen: set[str] = set()
        for match in re.finditer(r"([A-Z][A-Za-z0-9_]*)\.", expression):
            root = match.group(1)
            if root in seen:
                continue
            seen.add(root)
            if root not in model.entities:
                errors.append(ValidationError(
                    path=path_prefix,
                    message=(
                        f"Expression references unknown entity '{root}' "
                        f"in '{expression}'"
                    ),
                ))
        return errors

    @staticmethod
    def _model_id_matches(target: str, candidates: set[str]) -> bool:
        """Check whether a semantic-layer model reference matches a SQLMesh model.

        Matching is dialect-tolerant: identifiers are compared with surrounding
        quotes removed and case-folded. A short reference such as
        ``sushi.customers`` also matches a fully-qualified candidate like
        ``"catalog"."sushi"."customers"`` via dotted-suffix comparison.
        """
        t = CrossRefValidator._normalize_model_id(target)
        for candidate in candidates:
            c = CrossRefValidator._normalize_model_id(candidate)
            if t == c or c.endswith("." + t):
                return True
        return False

    @staticmethod
    def _normalize_model_id(identifier: str) -> str:
        """Normalize a model identifier for comparison (strip quotes, casefold)."""
        return identifier.replace('"', "").replace("`", "").strip().casefold()

    @staticmethod
    def _entity_has_attribute(entity: Entity, attr_name: str) -> bool:
        """Check if an attribute name exists on the entity.

        Checks key mappings, attributes, and foreign keys.
        """
        # Check key mapping
        if attr_name in entity.key:
            return True
        # Check attributes
        for attr in entity.attributes:
            if attr.name == attr_name:
                return True
        # Check foreign keys
        for fk in entity.foreign_keys:
            if fk.name == attr_name:
                return True
        return False

    @staticmethod
    def _validate_concept_attr_ref(
        expression: str,
        model: SemanticModel,
        path_prefix: str,
    ) -> list[ValidationError]:
        """Validate that a 'Concept.attribute' reference in an expression
        points to an existing entity and attribute."""
        errors: list[ValidationError] = []

        # Match patterns like "EntityName.attr_name"
        pattern = r"([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
        for match in re.finditer(pattern, expression):
            ent_name, attr_name = match.group(1), match.group(2)

            if ent_name not in model.entities:
                errors.append(ValidationError(
                    path=path_prefix,
                    message=(
                        f"Expression references unknown entity '{ent_name}' "
                        f"in '{expression}'"
                    ),
                ))
                continue

            entity = model.entities[ent_name]
            if not CrossRefValidator._entity_has_attribute(entity, attr_name):
                errors.append(ValidationError(
                    path=path_prefix,
                    message=(
                        f"Expression references unknown attribute "
                        f"'{attr_name}' on entity '{ent_name}'"
                    ),
                ))

        return errors

    @staticmethod
    def _extract_metric_names(
        formula: str, model: SemanticModel
    ) -> list[str]:
        """Extract referenced metric names from a derived formula.

        Matches identifiers that correspond to known metric names in the model.
        Falls back to extracting all snake_case identifiers if no known metrics
        are found, to catch references to not-yet-defined metrics.
        """
        # Extract all snake_case identifiers (potential metric names)
        pattern = r"([a-z][a-z0-9_]*)"
        candidates = re.findall(pattern, formula)

        # Filter out SQL keywords and operators
        sql_keywords = {
            "and", "or", "not", "null", "true", "false", "case", "when",
            "then", "else", "end", "as", "in", "between", "like", "is",
            "sum", "count", "avg", "min", "max", "distinct", "if", "coalesce",
            "cast", "convert", "abs", "round", "ceil", "floor", "div", "mod",
        }
        return [c for c in candidates if c not in sql_keywords]
