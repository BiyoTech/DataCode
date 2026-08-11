"""End-to-end tests using sushi_retail.yaml.

Follows the retrieval sequence described in design doc §7.2 to verify
that the full pipeline (load → validate → search → detail) works correctly
with the real example ontology.
"""

from __future__ import annotations

import pytest

from src.cross_ref_validator import CrossRefValidator
from src.loader import ModelRegistry
from src.semantic_search import SemanticSearchService


@pytest.fixture
def e2e_registry(workspace_dir):
    return ModelRegistry(workspace_dir)


@pytest.fixture
def e2e_search(e2e_registry):
    return SemanticSearchService(e2e_registry)


# ============================================================
# Full pipeline: load → validate → search → detail
# ============================================================

class TestEndToEndPipeline:
    def test_step0_load_and_validate(self, e2e_registry: ModelRegistry):
        """Step 0: Load the sushi_retail model and validate cross-references."""
        model = e2e_registry.get_model("sushi_retail")
        assert model.name == "sushi_retail"
        assert len(model.entities) == 4
        assert len(model.relationships) == 6
        assert len(model.metrics) == 5

        errors = CrossRefValidator.validate(model)
        assert errors == [], (
            f"sushi_retail should have zero validation errors, got: "
            f"{[f'{e.path}: {e.message}' for e in errors]}"
        )

    def test_step1_list_models(self, e2e_registry: ModelRegistry):
        """Step 1: Agent discovers available ontologies."""
        models = e2e_registry.list_models()
        assert len(models) == 1
        assert models[0].name == "sushi_retail"
        assert models[0].description is not None

    def test_step2_search_entities_active_customer(
        self, e2e_search: SemanticSearchService
    ):
        """Step 2a: Search for 'ActiveCustomer' — should match Customer's
        sub-concept."""
        results = e2e_search.search_entities("sushi_retail", "ActiveCustomer")
        assert len(results) >= 1
        # Customer should be in the results (ActiveCustomer is its sub-concept)
        customer = next(r for r in results if r.name == "Customer")
        assert customer.match_score > 0
        assert "sub-concept" in customer.match_reason.lower()

    def test_step2_search_entities_customer(self, e2e_search: SemanticSearchService):
        """Step 2b: Search for 'Customer' — exact match."""
        results = e2e_search.search_entities("sushi_retail", "Customer")
        assert len(results) >= 1
        customer = next(r for r in results if r.name == "Customer")
        assert customer.match_score >= 100  # exact match

    def test_step2_search_metrics_orders(self, e2e_search: SemanticSearchService):
        """Step 2c: Search for 'total_orders' — exact match."""
        results = e2e_search.search_metrics("sushi_retail", "total_orders")
        assert len(results) >= 1
        total_orders = next(r for r in results if r.name == "total_orders")
        assert total_orders.aggregate == "COUNT"
        assert total_orders.expression == "Order.order_id"
        assert total_orders.entity == "Order"

    def test_step3_get_entity_detail_customer(
        self, e2e_search: SemanticSearchService
    ):
        """Step 3a: Get Customer detail — should include ActiveCustomer
        sub-concept and Customer.places downstream relationship."""
        detail = e2e_search.get_entity_detail("sushi_retail", "Customer")

        # Sub-concept
        sc_names = [sc.name for sc in detail.sub_concepts]
        assert "ActiveCustomer" in sc_names
        active_sc = next(sc for sc in detail.sub_concepts if sc.name == "ActiveCustomer")
        assert "EXISTS" in str(active_sc.derived_by)

        # Downstream relationship
        downstream_names = [r.relationship_name for r in detail.downstream_relationships]
        assert "Customer.places" in downstream_names
        places_rel = next(
            r for r in detail.downstream_relationships
            if r.relationship_name == "Customer.places"
        )
        assert places_rel.target_entity == "Order"
        assert places_rel.join_conditions == [["customer_id", "customer_id"]]

    def test_step3_get_entity_detail_order(
        self, e2e_search: SemanticSearchService
    ):
        """Step 3b: Get Order detail — should include key mapping, attributes,
        and foreign keys."""
        detail = e2e_search.get_entity_detail("sushi_retail", "Order")

        # Key mapping (concept attr -> physical column)
        assert detail.key_mapping == {"order_id": "id"}

        # Model ref
        assert detail.model_ref == "sushi.orders"

        # Attributes
        attr_names = [a.name for a in detail.attributes]
        assert "event_date" in attr_names
        event_date = next(a for a in detail.attributes if a.name == "event_date")
        assert event_date.type == "time"

        # Foreign keys
        fk_names = [fk.name for fk in detail.foreign_keys]
        assert "customer_id" in fk_names
        assert "waiter_id" in fk_names

    def test_step3_get_metric_detail_total_orders(
        self, e2e_search: SemanticSearchService
    ):
        """Step 3c: Get total_orders metric detail."""
        detail = e2e_search.get_metric_detail("sushi_retail", "total_orders")
        assert detail.aggregate == "COUNT"
        assert detail.expression == "Order.order_id"
        assert detail.entity == "Order"

    def test_step4_get_entity_graph(self, e2e_search: SemanticSearchService):
        """Step 4: Get relationship graph for Customer at depth 2."""
        graph = e2e_search.get_entity_graph("sushi_retail", "Customer", depth=2)
        assert graph.center == "Customer"
        # At depth 2, we should reach OrderItem (Customer -> Order -> OrderItem)
        assert "Order" in graph.nodes
        assert "OrderItem" in graph.nodes
        # Should have the Customer.places edge
        assert any(
            e.relationship == "Customer.places" for e in graph.edges
        )


# ============================================================
# Complete retrieval sequence (design doc §7.2)
# ============================================================

class TestFullRetrievalSequence:
    """Simulates the complete Agent retrieval sequence from design doc §7.2."""

    def test_complete_workflow(self, e2e_search: SemanticSearchService):
        """Reproduce the exact sequence:

        1. list_models → sushi_retail
        2. search_entities("活跃客户") → ActiveCustomer/Customer
           search_metrics("订单数量") → total_orders
        3. get_entity_detail("ActiveCustomer" → via Customer)
           get_entity_detail("Order")
           get_metric_detail("total_orders")
        """
        # Step 1: list_models
        # (already tested above, skip for brevity)

        # Step 2: search
        entities = e2e_search.search_entities("sushi_retail", "Customer")
        assert len(entities) >= 1

        metrics = e2e_search.search_metrics("sushi_retail", "total_orders")
        assert len(metrics) >= 1

        # Step 3: detail
        customer_detail = e2e_search.get_entity_detail("sushi_retail", "Customer")
        order_detail = e2e_search.get_entity_detail("sushi_retail", "Order")
        metric_detail = e2e_search.get_metric_detail("sushi_retail", "total_orders")

        # Verify enough context for SQL generation
        # 1. Customer.places JOIN condition
        places = next(
            r for r in customer_detail.downstream_relationships
            if r.relationship_name == "Customer.places"
        )
        assert places.join_conditions == [["customer_id", "customer_id"]]
        assert places.target_entity == "Order"

        # 2. Order key mapping and model
        assert order_detail.model_ref == "sushi.orders"
        assert order_detail.key_mapping == {"order_id": "id"}

        # 3. Metric definition
        assert metric_detail.aggregate == "COUNT"
        assert metric_detail.expression == "Order.order_id"

        # 4. ActiveCustomer sub-concept derived_by
        active = next(
            sc for sc in customer_detail.sub_concepts
            if sc.name == "ActiveCustomer"
        )
        assert "EXISTS" in str(active.derived_by)
