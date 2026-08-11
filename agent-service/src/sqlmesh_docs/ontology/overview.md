# Ontology: Semantic Layer Overview

## What is the Ontology Semantic Layer?

The Ontology Semantic Layer bridges the gap between **raw SQLMesh physical models** (tables, columns, SQL) and **business meaning** ("what does this table represent?", "how are customers related to orders?", "how do I express this in natural language?").

Without a semantic layer, an AI Agent or business user looking at a table called `sushi.customers` has no way of knowing:
- That `sushi.customers` represents a "Customer" business concept
- That `customer_id` links to `sushi.orders`
- That "total orders" is a metric they should query with `COUNT(Order.order_id)`
- That "client" and "patron" are synonyms for "Customer"

The ontology fills this gap with a **single YAML file per business domain** that declares entities, relationships, metrics, and AI hints in one place.

## Core Design Principles

| Principle | Explanation |
|-----------|-------------|
| **Decoupled from SQLMesh** | Ontology files live under `ontology/` and never modify existing MODEL/METRIC definitions |
| **One file = one domain** | A single `{name}.yaml` defines all entities, relationships, and metrics for a business domain |
| **Single gateway per ontology** | An ontology declares one `gateway` and may only reference models from that gateway; different ontologies can share the same gateway |
| **Dual mapping** | Every entity declares both its **business concept** and its **physical model** in the same definition |
| **AI-Native** | Structured `ai_context` annotations (synonyms, instructions, examples) at every level |
| **OSI-compatible** | Concept model aligns with OSI ontology standards (EntityType, ValueType, ManyToOne, verbalizes) |

## File Location and Naming

```
{workspace}/
  ontology/                    # All ontology files go here
    sushi_retail.yaml          # The file stem IS the ontology name
    finance.yaml
    marketing.yaml
```

The **file name** (without `.yaml`) is the ontology name — do **not** write a `name: ...` field inside the YAML. The name is injected automatically when the file is loaded.

## File Structure

A complete ontology YAML file has five top-level sections:

```yaml
version: "0.1.0"
gateway: postgres                     # the SQLMesh gateway this ontology is built on
description: >
  Business-friendly description of this domain.
ai_context:                           # optional global AI hints
  instructions: "Use this for ..."
  synonyms: [term1, term2]

entities: [...]                       # business concepts mapped to SQLMesh models
relationships: [...]                  # semantic relationships with JOIN conditions
metrics: [...]                        # business metrics with aggregation logic
```

## Gateway Binding

SQLMesh loads a **separate context per gateway**, and external models are only
visible in the gateway they are bound to (plain base models without a gateway
are shared across every gateway). To keep an ontology internally consistent:

- Declare a top-level **`gateway`** field naming the SQLMesh gateway the
  ontology is built on (e.g. `postgres`, `mysql`).
- **All entity-backing models must belong to that single gateway.** An ontology
  may **not** mix models from two different gateways — validation rejects it.
- Base models (no gateway) are gateway-agnostic and always compatible.
- **Different ontologies may share the same gateway.** The single-gateway rule
  is per-ontology; there is no cross-ontology uniqueness constraint. Split a
  domain that spans two gateways into two ontology files, one per gateway.

Validation (see the ontology modeling skill) resolves each `Entity.model` to
its gateway via the live SQLMesh workspace and reports an error when models
from multiple gateways are mixed, or when an entity's gateway differs from the
declared `gateway`.

## How the System Works

```
          ontology/sushi_retail.yaml
                    │
                    ▼
┌─────────────────────────────────────────┐
│  ModelRegistry(workspace_path)          │
│    • Scans ontology/ for *.yaml          │
│    • Parses each file into SemanticModel │
└──────────────────┬──────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────┐
│  SemanticSearchService(registry)        │
│    • search_entities()                  │
│    • search_metrics()                   │
│    • search_relationships()             │
│    • get_entity_detail()                │
│    • get_metric_detail()                │
│    • get_entity_graph()                 │
│                                         │
│  Used by AI Agents for on-demand        │
│  semantic retrieval.                    │
└─────────────────────────────────────────┘
```

## Key Concepts at a Glance

| Concept | What it defines | Example |
|---------|----------------|--------|
| **Entity** | Business concept + physical table mapping + attributes + keys | `Customer` → `sushi.customers` |
| **Attribute** | A column of an entity, with type and AI hints | `status` (categorical), `event_date` (time) |
| **ForeignKey** | A column that references another entity's key | `customer_id` → `Customer.customer_id` |
| **SubConcept** | A derived subset of an entity | `ActiveCustomer` = `Customer` with at least one order |
| **Relationship** | How two entities are connected + the JOIN condition | `Customer.places → Order` with `[customer_id, customer_id]` |
| **Metric** | A measurable business value with aggregation | `total_orders` = `COUNT(Order.order_id)` |
| **AI Context** | Structured hints for AI: synonyms, instructions, examples | `synonyms: [client, account, patron]` |

## AI Agent Workflow

AI Agents interact with the ontology through the `SemanticSearchService` in an **on-demand retrieval** pattern:

```
Step 1: list_models()
        → Find which ontology matches the user's domain

Step 2: search_entities("order")
        search_metrics("revenue")
        → Narrow down to specific entities/metrics

Step 3: get_entity_detail("Order")
        get_metric_detail("total_revenue")
        → Retrieve JOIN conditions, aggregation expressions

Step 4: LLM generates SQL based on retrieved semantic information
```

This is more token-efficient than dumping the entire ontology — the agent only retrieves what it needs.

## Next Steps

- [Entities](./entities.md) — How to define business concepts and map them to physical models
- [Relationships](./relationships.md) — How to declare semantic relationships with JOIN conditions
- [Metrics](./metrics.md) — How to define business metrics with aggregation
- [AI Context](./ai_context.md) — How to annotate your ontology for AI understanding
