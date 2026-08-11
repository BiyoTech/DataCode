# Metrics

A **Metric** is a measurable business value defined with an aggregation function, an expression, and a target entity. There are four metric patterns, from simple aggregations to derived calculations to mappings on top of SQLMesh METRIC definitions.

## Metric Schema

```yaml
metrics:
  - name: <string>              # Metric name (lowercase, snake_case convention)
    description: <string>       # Human-readable description

    # ─── Pattern 1 & 2: Base / Filtered Metrics ───
    aggregate: COUNT | SUM | AVG | MIN | MAX | COUNT_DISTINCT
    expression: <Concept.attribute>  # Dot notation: "Order.order_id"
    entity: <EntityName>            # Primary entity (for single-entity metrics)
    entities: [<EntityName>, ...]   # Multi-entity metrics (used with `filter`)

    filter: <string>            # Pattern 2 only: semantic filter condition

    # ─── Pattern 3: Derived Metrics ───
    derived:
      formula: <expression>     # Arithmetic on other metric names

    # ─── Pattern 4: Mapped Metrics ───
    maps_to: <string>           # SQLMesh METRIC name to reference

    # ─── AI Context ───
    ai_context: <string | AIContext>  # Synonyms, examples, instructions
```

## Metric Patterns

### Pattern 1: Base Metric

Define a new metric directly in the ontology with aggregation and expression.

```yaml
metrics:
  - name: total_orders
    description: Total number of orders
    aggregate: COUNT
    expression: Order.order_id
    entity: Order
    ai_context:
      synonyms: [order count, number of orders, total purchases]
```

```yaml
  - name: total_ordered_items
    description: Total quantity of items ordered
    aggregate: SUM
    expression: OrderItem.quantity
    entity: OrderItem
    ai_context:
      synonyms: [items sold, quantity ordered]
```

### Pattern 2: Filtered Metric

A base metric with an additional semantic filter condition. The `entities` field lists all entities referenced in the filter.

```yaml
  - name: total_orders_from_active_customers
    description: Orders placed by customers with ACTIVE status
    aggregate: COUNT
    expression: Order.order_id
    filter: "Customer.status = 'ACTIVE'"         # Semantic filter
    entities: [Order, Customer]                   # All referenced entities
    ai_context:
      synonyms: [active orders, active customer orders]
```

**Filter syntax**: Filters use entity attribute references in the form `EntityName.attribute`. They are expressed at the **concept level**, not the SQL level — the system resolves them to physical columns automatically.

### Pattern 3: Derived Metric

A metric computed from other metrics using arithmetic. The `derived.formula` references other metric names.

```yaml
  - name: items_per_order
    description: Average number of items per order
    derived:
      formula: total_ordered_items / total_orders
    entities: [OrderItem, Order]
    ai_context:
      synonyms: [average items, items per purchase]
```

Supported operators in `derived.formula`:
- `+`, `-`, `*`, `/` — arithmetic
- `()` — grouping
- Other metric names — resolved to their aggregate expressions

Complex example:

```yaml
  - name: revenue_per_active_customer
    description: Average revenue from active customers
    derived:
      formula: total_revenue / active_customer_count
    entities: [Customer]
```

### Pattern 4: Mapped Metric (SQLMesh METRIC)

Reference an existing SQLMesh `METRIC(name ...)` definition without redefining it. The `maps_to` field carries the SQLMesh metric name; only AI annotations are added at the ontology level.

```yaml
  - name: sushi_total_orders
    description: Total orders as defined by SQLMesh METRIC
    maps_to: total_orders               # Matches METRIC(name total_orders)
    ai_context:
      examples:
        - "How many orders were placed last week?"
        - "Show order count by day for the past month"
```

> **Important**: `maps_to` metrics do NOT require `aggregate`, `expression`, `entity`, or `filter` — all that logic lives in the SQLMesh `METRIC(name ...)` definition. The ontology only adds discoverability and AI context.

## Supported Aggregate Functions

| Function | Description |
|----------|-------------|
| `COUNT` | Count of rows |
| `COUNT_DISTINCT` | Count of distinct values |
| `SUM` | Sum of numeric values |
| `AVG` | Average of numeric values |
| `MIN` | Minimum value |
| `MAX` | Maximum value |

## Expression Syntax

Metric expressions use **concept-level dot notation**:

```
EntityName.attributeName
```

Examples:
- `Order.order_id` — COUNT target
- `OrderItem.quantity` — SUM target
- `Customer.revenue` — aggregation on numeric attribute

The system resolves these to the physical column names from the entity's attribute definitions at query time.

## Metric Example Gallery

### Single-entity base metric
```yaml
- name: total_orders
  aggregate: COUNT
  expression: Order.order_id
  entity: Order
```

### Multi-entity filtered metric
```yaml
- name: vip_revenue
  description: Total revenue from VIP customers
  aggregate: SUM
  expression: Order.amount
  filter: "Customer.status = 'VIP'"
  entities: [Order, Customer]
```

### Time-filtered metric
```yaml
- name: orders_last_30_days
  description: Orders in the last 30 days
  aggregate: COUNT
  expression: Order.order_id
  filter: "Order.event_date >= DATE_SUB(CURRENT_DATE, 30)"
  entity: Order
```

### Derived metric with division
```yaml
- name: conversion_rate
  description: Percentage of customers who placed an order
  derived:
    formula: active_customers / total_customers * 100
  entities: [Customer]
```

### Mapped metric (SQLMesh reference)
```yaml
- name: sushi_revenue
  maps_to: total_revenue
  ai_context:
    synonyms: [sales, income, earnings]
    examples:
      - "What was total revenue last quarter?"
```

## Best Practices

1. **Use `entity` for single-entity metrics, `entities` for multi-entity or filtered metrics** — this distinction helps the query planner
2. **Prefer `maps_to` over redefining a metric** — if a SQLMesh METRIC already exists, add only AI context in the ontology
3. **Always add `ai_context.synonyms`** — they are used by the semantic search scoring to match user queries
4. **Filter conditions use concept names, not physical column names** — `"Customer.status"`, not `"customers.status"`
5. **Derived formula metrics are computed at the concept level** — the system resolves them to SQL subqueries
6. **Metric names follow `snake_case` convention** — `total_orders`, `items_per_order`
