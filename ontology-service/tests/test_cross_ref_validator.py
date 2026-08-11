"""Tests for the CrossRefValidator."""

from __future__ import annotations

import pytest

from src.cross_ref_validator import CrossRefValidator
from src.models import SemanticModel


@pytest.fixture
def valid_model(sushi_model: SemanticModel) -> SemanticModel:
    """The sushi_retail model should pass validation with zero errors."""
    return sushi_model


# ============================================================
# Valid model
# ============================================================

class TestValidModel:
    def test_sushi_retail_no_errors(self, valid_model: SemanticModel):
        errors = CrossRefValidator.validate(valid_model)
        assert errors == [], (
            f"Expected no validation errors, got: "
            f"{[f'{e.path}: {e.message}' for e in errors]}"
        )


# ============================================================
# Relationship reference errors
# ============================================================

class TestRelationshipErrors:
    def test_unknown_from_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[{"name": "Order", "type": "EntityType"}],
            relationships=[
                {
                    "name": "BadRel",
                    "from": "NonExistent",
                    "to": "Order",
                    "verbalizes": ["test"],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        rel_errors = [e for e in errors if "BadRel" in e.path]
        assert len(rel_errors) >= 1
        assert "NonExistent" in rel_errors[0].message

    def test_unknown_to_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[{"name": "Customer", "type": "EntityType"}],
            relationships=[
                {
                    "name": "BadRel",
                    "from": "Customer",
                    "to": "NonExistent",
                    "verbalizes": ["test"],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        rel_errors = [e for e in errors if "BadRel" in e.path]
        assert len(rel_errors) >= 1

    def test_join_attribute_not_found(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Customer",
                    "type": "EntityType",
                    "key": {"customer_id": "customer_id"},
                },
                {
                    "name": "Order",
                    "type": "EntityType",
                    "key": {"order_id": "id"},
                },
            ],
            relationships=[
                {
                    "name": "Customer.places",
                    "from": "Customer",
                    "to": "Order",
                    "join": [["nonexistent_attr", "order_id"]],
                    "verbalizes": ["{Customer} places {Order}"],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        join_errors = [e for e in errors if "join" in e.path]
        assert len(join_errors) >= 1
        assert "nonexistent_attr" in join_errors[0].message


# ============================================================
# Foreign key reference errors
# ============================================================

class TestForeignKeyErrors:
    def test_unknown_reference_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Order",
                    "type": "EntityType",
                    "key": {"order_id": "id"},
                    "foreign_keys": [
                        {
                            "name": "customer_id",
                            "column": "customer_id",
                            "references": "NonExistent.customer_id",
                        },
                    ],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        fk_errors = [e for e in errors if "foreign_keys" in e.path]
        assert len(fk_errors) >= 1
        assert "NonExistent" in fk_errors[0].message

    def test_unknown_reference_attribute(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Customer",
                    "type": "EntityType",
                    "key": {"customer_id": "customer_id"},
                },
                {
                    "name": "Order",
                    "type": "EntityType",
                    "key": {"order_id": "id"},
                    "foreign_keys": [
                        {
                            "name": "customer_id",
                            "column": "customer_id",
                            "references": "Customer.nonexistent_attr",
                        },
                    ],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        fk_errors = [e for e in errors if "foreign_keys" in e.path]
        assert len(fk_errors) >= 1

    def test_malformed_reference(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Order",
                    "type": "EntityType",
                    "key": {"order_id": "id"},
                    "foreign_keys": [
                        {
                            "name": "bad_fk",
                            "column": "x",
                            "references": "NoDotHere",
                        },
                    ],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        fk_errors = [e for e in errors if "foreign_keys" in e.path]
        assert len(fk_errors) >= 1


# ============================================================
# Metric reference errors
# ============================================================

class TestMetricErrors:
    def test_unknown_metric_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[{"name": "Order", "type": "EntityType"}],
            metrics=[
                {
                    "name": "bad_metric",
                    "aggregate": "COUNT",
                    "expression": "Order.order_id",
                    "entity": "NonExistent",
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        metric_errors = [e for e in errors if "metric" in e.path.lower()]
        assert len(metric_errors) >= 1

    def test_unknown_expression_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[{"name": "Order", "type": "EntityType"}],
            metrics=[
                {
                    "name": "bad_metric",
                    "aggregate": "COUNT",
                    "expression": "NonExistent.some_attr",
                    "entity": "Order",
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert any("NonExistent" in e.message for e in errors)

    def test_derived_formula_unknown_metric(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            metrics=[
                {
                    "name": "derived_metric",
                    "derived": {"formula": "nonexistent_metric / 2"},
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        derived_errors = [e for e in errors if "derived" in e.path]
        assert len(derived_errors) >= 1

    def test_maps_to_not_validated(self):
        """maps_to is not validated when the SQLMesh metric set is not provided."""
        model = SemanticModel(
            version="0.1.0",
            name="test",
            metrics=[
                {
                    "name": "mapped_metric",
                    "maps_to": "some_sqlmesh_metric",
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        # Should NOT produce errors for maps_to
        maps_to_errors = [e for e in errors if "maps_to" in e.path]
        assert maps_to_errors == []


# ============================================================
# Metric filter reference errors
# ============================================================

class TestMetricFilterErrors:
    def test_unknown_filter_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[{"name": "Order", "type": "EntityType", "key": {"order_id": "id"}}],
            metrics=[
                {
                    "name": "bad_metric",
                    "aggregate": "COUNT",
                    "expression": "Order.order_id",
                    "filter": "Customer.status = 'ACTIVE'",
                    "entity": "Order",
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        filter_errors = [e for e in errors if e.path.endswith(".filter")]
        assert len(filter_errors) >= 1
        assert "Customer" in filter_errors[0].message


# ============================================================
# Entity inheritance / derivation errors
# ============================================================

class TestEntityInheritanceErrors:
    def test_extends_unknown_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Order", "type": "EntityType", "extends": ["NonExistent"]},
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert any("extends" in e.path and "NonExistent" in e.message for e in errors)

    def test_extends_primitive_value_type_allowed(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Amount", "type": "ValueType", "extends": ["Decimal"]},
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert [e for e in errors if "extends" in e.path] == []

    def test_extends_existing_entity_allowed(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Customer", "type": "EntityType"},
                {"name": "VipCustomer", "type": "EntityType", "extends": ["Customer"]},
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert [e for e in errors if "extends" in e.path] == []

    def test_identify_by_unknown_relationship(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Order", "type": "EntityType", "identify_by": ["Order.missing"]},
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert any("identify_by" in e.path for e in errors)

    def test_derived_by_unknown_root_entity(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Customer",
                    "type": "EntityType",
                    "derived_by": ["EXISTS(Ghost.places.Order)"],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert any("derived_by" in e.path and "Ghost" in e.message for e in errors)

    def test_sub_concept_derived_by_valid_root(self):
        """A valid OSI path expression referencing a known entity passes."""
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Customer",
                    "type": "EntityType",
                    "sub_concepts": [
                        {
                            "name": "ActiveCustomer",
                            "derived_by": "EXISTS(Customer.places.Order)",
                        },
                    ],
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        assert [e for e in errors if "derived_by" in e.path] == []


# ============================================================
# SQLMesh existence checks (opt-in)
# ============================================================

class TestSqlMeshModelExistence:
    def _model(self):
        return SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Customer", "type": "EntityType", "model": "sushi.customers"},
            ],
        )

    def test_missing_model_reports_error(self):
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_models={"sushi.orders"}
        )
        assert any(e.path.endswith(".model") and "sushi.customers" in e.message for e in errors)

    def test_existing_model_no_error(self):
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_models={"sushi.customers"}
        )
        assert [e for e in errors if e.path.endswith(".model")] == []

    def test_fqn_suffix_match(self):
        """A short name matches a fully-qualified, quoted candidate."""
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_models={'"memory"."sushi"."customers"'}
        )
        assert [e for e in errors if e.path.endswith(".model")] == []

    def test_skipped_when_set_is_none(self):
        errors = CrossRefValidator.validate(self._model(), sqlmesh_models=None)
        assert [e for e in errors if e.path.endswith(".model")] == []


class TestSqlMeshMetricExistence:
    def _model(self):
        return SemanticModel(
            version="0.1.0",
            name="test",
            metrics=[
                {"name": "sushi_total_orders", "maps_to": "total_orders"},
            ],
        )

    def test_missing_metric_reports_error(self):
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_metrics={"other_metric"}
        )
        assert any(e.path.endswith(".maps_to") and "total_orders" in e.message for e in errors)

    def test_existing_metric_no_error(self):
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_metrics={"total_orders"}
        )
        assert [e for e in errors if e.path.endswith(".maps_to")] == []

    def test_case_insensitive_match(self):
        errors = CrossRefValidator.validate(
            self._model(), sqlmesh_metrics={"TOTAL_ORDERS"}
        )
        assert [e for e in errors if e.path.endswith(".maps_to")] == []



# ============================================================
# Multi-error collection
# ============================================================

class TestMultiErrorCollection:
    def test_collects_multiple_errors(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {
                    "name": "Order",
                    "type": "EntityType",
                    "key": {"order_id": "id"},
                    "foreign_keys": [
                        {
                            "name": "fk1",
                            "column": "x",
                            "references": "BadEntity.bad_attr",
                        },
                    ],
                },
            ],
            relationships=[
                {
                    "name": "BadRel",
                    "from": "BadFrom",
                    "to": "BadTo",
                    "verbalizes": ["test"],
                },
            ],
            metrics=[
                {
                    "name": "bad_metric",
                    "aggregate": "COUNT",
                    "expression": "BadEntity.attr",
                    "entity": "BadEntity",
                },
            ],
        )
        errors = CrossRefValidator.validate(model)
        # Should have multiple errors (not just the first one)
        assert len(errors) >= 3
