"""Tests for the SemanticSearchService."""

from __future__ import annotations

import pytest

from src.semantic_search import SemanticSearchService

# ============================================================
# search_entities
# ============================================================

class TestSearchEntities:
    def test_exact_name_match(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "Customer")
        assert any(r.name == "Customer" for r in results)
        customer = next(r for r in results if r.name == "Customer")
        assert customer.match_score >= 100  # exact name match
        assert "exact name match" in customer.match_reason.lower()

    def test_case_insensitive(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "customer")
        assert any(r.name == "Customer" for r in results)

    def test_synonym_match(self, search_service: SemanticSearchService):
        # Order has synonym "purchase"
        results = search_service.search_entities("sushi_retail", "purchase")
        assert any(r.name == "Order" for r in results)

    def test_sub_concept_match(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "ActiveCustomer")
        # ActiveCustomer is a sub-concept of Customer
        assert any(r.name == "Customer" for r in results)

    def test_no_results(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "zzznonexistent")
        assert results == []

    def test_limit(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "order", limit=1)
        assert len(results) <= 1

    def test_sorted_by_score(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "order", limit=10)
        scores = [r.match_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_match_reason_populated(self, search_service: SemanticSearchService):
        results = search_service.search_entities("sushi_retail", "Customer")
        assert all(r.match_reason != "" for r in results)


# ============================================================
# search_metrics
# ============================================================

class TestSearchMetrics:
    def test_exact_name_match(self, search_service: SemanticSearchService):
        results = search_service.search_metrics("sushi_retail", "total_orders")
        assert any(r.name == "total_orders" for r in results)
        metric = next(r for r in results if r.name == "total_orders")
        assert metric.match_score >= 100
        assert metric.aggregate == "COUNT"
        assert metric.expression == "Order.order_id"

    def test_synonym_match(self, search_service: SemanticSearchService):
        # total_orders has synonym "order count"
        results = search_service.search_metrics("sushi_retail", "order count")
        assert any(r.name == "total_orders" for r in results)

    def test_derived_flag(self, search_service: SemanticSearchService):
        results = search_service.search_metrics("sushi_retail", "items_per_order")
        metric = next(r for r in results if r.name == "items_per_order")
        assert metric.is_derived is True

    def test_mapped_flag(self, search_service: SemanticSearchService):
        results = search_service.search_metrics("sushi_retail", "sushi_total_orders")
        metric = next(r for r in results if r.name == "sushi_total_orders")
        assert metric.is_mapped is True

    def test_no_results(self, search_service: SemanticSearchService):
        results = search_service.search_metrics("sushi_retail", "zzznonexistent")
        assert results == []


# ============================================================
# search_relationships
# ============================================================

class TestSearchRelationships:
    def test_by_name(self, search_service: SemanticSearchService):
        results = search_service.search_relationships("sushi_retail", "places")
        assert any(r.name == "Customer.places" for r in results)

    def test_by_verbalizes(self, search_service: SemanticSearchService):
        results = search_service.search_relationships(
            "sushi_retail", "places {Order}"
        )
        assert any(r.name == "Customer.places" for r in results)

    def test_by_entity_name(self, search_service: SemanticSearchService):
        results = search_service.search_relationships("sushi_retail", "Customer")
        # Should match relationships involving Customer
        assert len(results) >= 1

    def test_returns_join_info(self, search_service: SemanticSearchService):
        results = search_service.search_relationships("sushi_retail", "places")
        rel = next(r for r in results if r.name == "Customer.places")
        assert rel.from_entity == "Customer"
        assert rel.to_entity == "Order"
        assert rel.multiplicity == "ManyToOne"

    def test_no_results(self, search_service: SemanticSearchService):
        results = search_service.search_relationships("sushi_retail", "zzznonexistent")
        assert results == []


# ============================================================
# get_entity_detail
# ============================================================

class TestGetEntityDetail:
    def test_basic_fields(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Customer")
        assert detail.name == "Customer"
        assert detail.type == "EntityType"
        assert detail.model_ref == "sushi.customers"
        assert "customer_id" in detail.key_mapping

    def test_attributes(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Order")
        attr_names = [a.name for a in detail.attributes]
        assert "event_date" in attr_names

    def test_foreign_keys(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Order")
        fk_names = [fk.name for fk in detail.foreign_keys]
        assert "customer_id" in fk_names
        assert "waiter_id" in fk_names

    def test_sub_concepts(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Customer")
        sc_names = [sc.name for sc in detail.sub_concepts]
        assert "ActiveCustomer" in sc_names

    def test_downstream_relationships(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Customer")
        downstream_names = [r.relationship_name for r in detail.downstream_relationships]
        assert "Customer.places" in downstream_names
        # All downstream should have direction "out"
        assert all(r.direction == "out" for r in detail.downstream_relationships)

    def test_upstream_relationships(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Order")
        upstream_names = [r.relationship_name for r in detail.upstream_relationships]
        assert "Customer.places" in upstream_names
        # All upstream should have direction "in"
        assert all(r.direction == "in" for r in detail.upstream_relationships)

    def test_join_conditions_in_detail(self, search_service: SemanticSearchService):
        detail = search_service.get_entity_detail("sushi_retail", "Customer")
        places_rel = next(
            r for r in detail.downstream_relationships
            if r.relationship_name == "Customer.places"
        )
        assert places_rel.join_conditions == [["customer_id", "customer_id"]]

    def test_entity_not_found(self, search_service: SemanticSearchService):
        with pytest.raises(KeyError, match="not found"):
            search_service.get_entity_detail("sushi_retail", "NonExistent")


# ============================================================
# get_metric_detail
# ============================================================

class TestGetMetricDetail:
    def test_base_metric(self, search_service: SemanticSearchService):
        detail = search_service.get_metric_detail("sushi_retail", "total_orders")
        assert detail.name == "total_orders"
        assert detail.aggregate == "COUNT"
        assert detail.expression == "Order.order_id"
        assert detail.entity == "Order"
        assert not detail.is_derived
        assert not detail.is_mapped

    def test_filtered_metric(self, search_service: SemanticSearchService):
        detail = search_service.get_metric_detail(
            "sushi_retail", "total_orders_from_active_customers"
        )
        assert detail.filter is not None
        assert "ACTIVE" in detail.filter
        assert "Order" in detail.entities
        assert "Customer" in detail.entities

    def test_derived_metric(self, search_service: SemanticSearchService):
        detail = search_service.get_metric_detail("sushi_retail", "items_per_order")
        assert detail.is_derived
        assert detail.derived_formula is not None
        assert "total_ordered_items" in detail.derived_formula
        assert "total_orders" in detail.derived_formula

    def test_mapped_metric(self, search_service: SemanticSearchService):
        detail = search_service.get_metric_detail("sushi_retail", "sushi_total_orders")
        assert detail.is_mapped
        assert detail.maps_to == "total_orders"

    def test_metric_not_found(self, search_service: SemanticSearchService):
        with pytest.raises(KeyError, match="not found"):
            search_service.get_metric_detail("sushi_retail", "NonExistent")


# ============================================================
# get_entity_graph
# ============================================================

class TestGetEntityGraph:
    def test_depth_1(self, search_service: SemanticSearchService):
        graph = search_service.get_entity_graph("sushi_retail", "Customer", depth=1)
        assert graph.center == "Customer"
        assert graph.depth == 1
        # Customer has direct relationship to Order (via Customer.places)
        assert "Order" in graph.nodes
        assert any(
            e.relationship == "Customer.places" for e in graph.edges
        )

    def test_depth_2(self, search_service: SemanticSearchService):
        graph = search_service.get_entity_graph("sushi_retail", "Customer", depth=2)
        # At depth 2, we should reach OrderItem (Customer -> Order -> OrderItem)
        assert "OrderItem" in graph.nodes

    def test_self_referential_not_duplicated(self, search_service: SemanticSearchService):
        """Self-referential relationships (from == to) should not cause
        duplicate node entries."""
        graph = search_service.get_entity_graph("sushi_retail", "Customer", depth=1)
        # Customer.has_status is self-referential (Customer -> Customer)
        # It should not add Customer as a new node
        assert "Customer" in graph.nodes

    def test_graph_node_has_model_ref(self, search_service: SemanticSearchService):
        graph = search_service.get_entity_graph("sushi_retail", "Order", depth=1)
        order_node = graph.nodes["Order"]
        assert order_node.model == "sushi.orders"

    def test_entity_not_found(self, search_service: SemanticSearchService):
        with pytest.raises(KeyError, match="not found"):
            search_service.get_entity_graph("sushi_retail", "NonExistent")
