# AI Context

**AI Context** (`ai_context`) is a structured annotation system that enriches ontology definitions with information specifically designed for AI Agent consumption. It can be attached at any level of an ontology — from the global model down to individual attributes and metrics.

## Why AI Context Matters

Raw schema information (column names, types, table names) tells an AI Agent **what** data exists. AI Context tells the agent **how a human thinks about** that data:

| Without AI Context | With AI Context |
|-------------------|-----------------|
| `sushi.customers.status VARCHAR` | `sushi.customers.status` = "Customer status (categorical)" — also known as "customer state", "account status" |
| ` COUNT(sushi.orders.id) ` | "Total number of orders" — also called "order count", "total purchases"; used to answer "how many orders were placed?" |

This dramatically improves AI-to-SQL translation accuracy and helps the agent understand which metrics/entities match a user's natural-language query.

## Where to Attach AI Context

| Attachment level | Field path | What it enhances |
|-----------------|-----------|-----------------|
| Global ontology | `ai_context` (top-level) | Overall domain understanding |
| Entity | `entities[].ai_context` | Entity name synonyms, usage guidance |
| Attribute | `entities[].attributes[].ai_context` | Column-level synonyms |
| Sub-concept | `entities[].sub_concepts[].ai_context` | Derived concept hints |
| Relationship | `relationships[].ai_context` | Relationship usage guidance |
| Metric | `metrics[].ai_context` | Metric synonyms, example questions |

## AI Context Formats

### Simple String (Legacy)

A plain English string — quick and easy but limited:

```yaml
ai_context: "Use this for sales analysis and revenue reporting"
```

### Structured Object (Recommended)

The structured form provides three distinct fields:

```yaml
ai_context:
  instructions: <string>    # Guidance on HOW to use this concept
  synonyms: [<string>, ...] # Alternative names / aliases
  examples: [<string>, ...] # Example natural-language questions
```

#### `instructions`
High-level guidance for the AI on when and how to use the concept.

```yaml
ai_context:
  instructions: >
    Use this model for sushi restaurant sales and customer analytics.
    All monetary amounts are in USD.
```

#### `synonyms`
Alternative names for the concept. These are indexed by the semantic search service and used for keyword matching. **The more synonyms you add, the better the AI can find the right concept.**

```yaml
ai_context:
  synonyms:
    - sushi analytics
    - restaurant data
    - food service metrics
```

Search scoring weights for synonyms: **80 points** (exact match), making them highly effective for disambiguation.

#### `examples`
Example natural-language questions that this concept can answer. These help the AI understand the concept's **intent** and **user scenarios**.

```yaml
ai_context:
  examples:
    - "How many orders were placed last week?"
    - "What is the revenue by customer region?"
    - "Show me the top 10 most active customers"
```

## Usage Examples

### At the Ontology Level

```yaml
description: Sushi restaurant retail analytics semantic model
ai_context:
  instructions: "Use this model for sushi restaurant sales and customer analytics"
  synonyms: [sushi analytics, restaurant data]
```

### At the Entity Level

```yaml
entities:
  - name: Customer
    ai_context:
      synonyms: [client, account, patron]
      instructions: "Customers can be ACTIVE or INACTIVE. Use status to filter."

  - name: Order
    ai_context:
      synonyms: [purchase, transaction, sale]
```

### At the Attribute Level

```yaml
attributes:
  - name: status
    column: status
    type: categorical
    ai_context:
      synonyms: [customer state, account status]

  - name: event_date
    column: event_date
    type: time
    granularity: day
    ai_context:
      synonyms: [order date, purchase date, transaction date]
```

### At the Metric Level

```yaml
metrics:
  - name: total_orders
    description: Total number of orders
    aggregate: COUNT
    expression: Order.order_id
    entity: Order
    ai_context:
      synonyms: [order count, number of orders, total purchases]
      examples:
        - "How many orders were placed this month?"
        - "Show order count by day"

  - name: items_per_order
    description: Average number of items per order
    derived:
      formula: total_ordered_items / total_orders
    entities: [OrderItem, Order]
    ai_context:
      synonyms: [average items, items per purchase, basket size]
      examples:
        - "What is the average basket size?"
        - "How many items per order on average?"
```

## How Semantic Search Uses AI Context

The `SemanticSearchService` indexes and weights AI Context fields during keyword searches. Here is the scoring priority:

| Match location | Score |
|----------------|-------|
| Exact name match | **100** |
| Sub-concept name match | **90** |
| `ai_context.synonyms` exact match | **80** |
| Partial name match | **50** |
| Relationship `verbalizes` match | **40** |
| `description` match | **20** |

This means a user searching for "client" will match `Customer` with score 80 (via synonym), even though the actual entity name is "Customer" not "client".

## Best Practices

1. **Add at least 3-5 synonyms per entity** — cover common business jargon, abbreviations, and alternative names
2. **Use `examples` for common user questions** — this is the most effective way to help AI match user intent
3. **Prefer the structured object format over plain strings** — it enables targeted AI behavior
4. **Keep `instructions` focused and domain-specific** — broad generic instructions add noise
5. **Add synonyms to commonly misinterpreted attributes** — e.g., "purchase date" → `event_date`, "account state" → `status`
6. **`examples` for mapped metrics (`maps_to`) are especially important** — they give context to metrics the ontology didn't define
