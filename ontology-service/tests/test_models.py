"""Tests for Pydantic v2 data models in models.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models import (
    AIContext,
    Attribute,
    Entity,
    Metric,
    OntologySchemaError,
    Relationship,
    SemanticModel,
)

# ============================================================
# AIContext
# ============================================================

class TestAIContext:
    def test_empty_defaults(self):
        ctx = AIContext()
        assert ctx.instructions is None
        assert ctx.synonyms == []
        assert ctx.examples == []

    def test_full_construction(self):
        ctx = AIContext(
            instructions="Use for analytics",
            synonyms=["sushi", "restaurant"],
            examples=["Show total sales"],
        )
        assert ctx.instructions == "Use for analytics"
        assert ctx.synonyms == ["sushi", "restaurant"]
        assert ctx.examples == ["Show total sales"]

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            AIContext(instructions="x", unknown_field="bad")


# ============================================================
# Attribute
# ============================================================

class TestAttribute:
    def test_defaults(self):
        attr = Attribute(name="status", column="status")
        assert attr.type == "categorical"
        assert attr.granularity is None
        assert attr.description is None
        assert attr.ai_context is None

    def test_with_ai_context_string(self):
        attr = Attribute(
            name="status", column="status", ai_context="customer state"
        )
        assert attr.ai_context == "customer state"

    def test_with_ai_context_object(self):
        attr = Attribute(
            name="status",
            column="status",
            ai_context=AIContext(synonyms=["customer state"]),
        )
        assert attr.ai_context.synonyms == ["customer state"]

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            Attribute(name="x", column="x", type="invalid_type")


# ============================================================
# Entity
# ============================================================

class TestEntity:
    def test_sub_concepts_list_to_dict(self):
        entity = Entity(
            name="Customer",
            type="EntityType",
            sub_concepts=[
                {"name": "ActiveCustomer", "derived_by": "EXISTS(...)"},
                {"name": "VIPCustomer", "derived_by": "status = 'VIP'"},
            ],
        )
        assert isinstance(entity.sub_concepts, dict)
        assert "ActiveCustomer" in entity.sub_concepts
        assert "VIPCustomer" in entity.sub_concepts
        assert entity.sub_concepts["ActiveCustomer"].derived_by == "EXISTS(...)"

    def test_sub_concepts_empty_default(self):
        entity = Entity(name="X", type="EntityType")
        assert entity.sub_concepts == {}

    def test_key_mapping(self):
        entity = Entity(
            name="Order",
            type="EntityType",
            key={"order_id": "id"},
        )
        assert entity.key == {"order_id": "id"}


# ============================================================
# Relationship (field alias for from/to)
# ============================================================

class TestRelationship:
    def test_alias_from_to(self):
        rel = Relationship(
            name="Customer.places",
            **{"from": "Customer", "to": "Order"},
            join=[["customer_id", "customer_id"]],
            multiplicity="ManyToOne",
            verbalizes=["{Customer} places {Order}"],
        )
        assert rel.from_entity == "Customer"
        assert rel.to_entity == "Order"

    def test_populate_by_name(self):
        rel = Relationship(
            name="Customer.places",
            from_entity="Customer",
            to_entity="Order",
            verbalizes=["{Customer} places {Order}"],
        )
        assert rel.from_entity == "Customer"

    def test_invalid_multiplicity(self):
        with pytest.raises(ValidationError):
            Relationship(
                name="X",
                **{"from": "A", "to": "B"},
                multiplicity="Invalid",
                verbalizes=["test"],
            )

    def test_verbalizes_required_as_list(self):
        rel = Relationship(
            name="X",
            **{"from": "A", "to": "B"},
        )
        assert rel.verbalizes == []


# ============================================================
# Metric
# ============================================================

class TestMetric:
    def test_base_metric(self):
        m = Metric(
            name="total_orders",
            aggregate="COUNT",
            expression="Order.order_id",
            entity="Order",
        )
        assert not m.is_derived()
        assert not m.is_mapped()

    def test_derived_metric(self):
        m = Metric(
            name="items_per_order",
            derived={"formula": "total_items / total_orders"},
        )
        assert m.is_derived()
        assert not m.is_mapped()
        assert m.derived.formula == "total_items / total_orders"

    def test_mapped_metric(self):
        m = Metric(
            name="sushi_total_orders",
            maps_to="total_orders",
        )
        assert not m.is_derived()
        assert m.is_mapped()
        assert m.maps_to == "total_orders"

    def test_invalid_aggregate(self):
        with pytest.raises(ValidationError):
            Metric(name="x", aggregate="MEDIAN", expression="Order.id")


# ============================================================
# SemanticModel (list-to-dict conversion + from_yaml)
# ============================================================

class TestSemanticModel:
    def test_entities_list_to_dict(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            entities=[
                {"name": "Customer", "type": "EntityType"},
                {"name": "Order", "type": "EntityType"},
            ],
        )
        assert isinstance(model.entities, dict)
        assert "Customer" in model.entities
        assert "Order" in model.entities

    def test_relationships_list_to_dict(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            relationships=[
                {
                    "name": "Customer.places",
                    "from": "Customer",
                    "to": "Order",
                    "verbalizes": ["{Customer} places {Order}"],
                },
            ],
        )
        assert isinstance(model.relationships, dict)
        assert "Customer.places" in model.relationships
        assert model.relationships["Customer.places"].from_entity == "Customer"

    def test_metrics_list_to_dict(self):
        model = SemanticModel(
            version="0.1.0",
            name="test",
            metrics=[
                {"name": "total_orders", "aggregate": "COUNT"},
            ],
        )
        assert isinstance(model.metrics, dict)
        assert "total_orders" in model.metrics

    def test_empty_collections_default(self):
        model = SemanticModel(version="0.1.0", name="test")
        assert model.entities == {}
        assert model.relationships == {}
        assert model.metrics == {}

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SemanticModel(
                version="0.1.0",
                name="test",
                unknown_field="bad",
            )

    def test_from_yaml_injects_name(self, tmp_path):
        yaml_path = tmp_path / "test_model.yaml"
        yaml_path.write_text(
            'version: "0.1.0"\n'
            'description: Test model\n',
            encoding="utf-8",
        )
        model = SemanticModel.from_yaml(yaml_path, name="test_model")
        assert model.name == "test_model"
        assert model.description == "Test model"

    def test_from_yaml_non_mapping_raises(self, tmp_path):
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            SemanticModel.from_yaml(yaml_path, name="bad")

    def test_from_yaml_schema_error_carries_field_details(self, tmp_path):
        # Reproduces the real-world failure: an attribute uses an unsupported
        # type 'timestamp' (allowed: categorical/time/numeric/text).
        yaml_path = tmp_path / "olist.yaml"
        yaml_path.write_text(
            'version: "0.1.0"\n'
            "entities:\n"
            "  - name: Order\n"
            "    type: EntityType\n"
            "    attributes:\n"
            "      - name: created_at\n"
            "        column: created_at\n"
            "        type: timestamp\n",
            encoding="utf-8",
        )

        with pytest.raises(OntologySchemaError) as raised:
            SemanticModel.from_yaml(yaml_path, name="olist")

        exc = raised.value
        assert exc.ontology_name == "olist"
        assert isinstance(exc, ValueError)  # backwards-compatible handling
        assert len(exc.errors) == 1
        err = exc.errors[0]
        assert err["location"] == "entities.Order.attributes[0].type"
        assert err["input"] == "timestamp"
        assert "categorical" in err["message"]
