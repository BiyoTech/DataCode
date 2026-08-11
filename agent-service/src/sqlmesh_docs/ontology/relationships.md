# Relationships

A **Relationship** defines how two entities are connected — both semantically (what the connection means in business terms) and physically (what SQL JOIN to use). Every relationship is bidirectional, with natural-language descriptions in both directions.

## Relationship Schema

```yaml
relationships:
  - name: <string>              # Full name, conventionally SourceEntity.relName
    description: <string>       # Human-readable description (optional)
    from: <EntityName>          # Source entity
    to: <EntityName>            # Target entity

    # ─── Physical JOIN Condition ───
    join:                       # List of [from_column, to_column] pairs
      - [<from_attr>, <to_attr>]
    # Multi-column JOIN example:
    #   - [customer_id, customer_id]
    #   - [order_date, event_date]

    # ─── Multiplicity ───
    multiplicity: ManyToOne | OneToOne

    # ─── Natural Language ───
    verbalizes:                 # REQUIRED — at least one direction
      - "{SourceEntity} verb {TargetEntity}"           # Forward
      - "{TargetEntity} is verb-ed by {SourceEntity}"  # Reverse

    # ─── Optional Features ───
    via_attribute: <string>     # When the relationship value comes from an attribute
    derived_by: [<string>]      # Derived relationship expressions (optional)
    requires: [<string>]        # Prerequisite relationships (optional)
    ai_context: <AIContext>     # Optional AI hints
```

## Relationship Types

### 1. Entity-to-Entity Relationships (Cross-entity Foreign Keys)

The most common type — connects two different entities via a foreign key.

```yaml
relationships:
  - name: Customer.places
    from: Customer
    to: Order
    join:
      - [customer_id, customer_id]
    multiplicity: ManyToOne
    verbalizes:
      - "{Customer} places {Order}"
      - "{Order} is placed by {Customer}"
```

### 2. Via-Attribute Relationships (Attribute as Value)

When you want to treat an entity's attribute as a relationship — useful for filtering or grouping by attribute values.

```yaml
relationships:
  - name: Customer.has_status
    from: Customer
    to: Customer                  # Self-reference — same entity
    join:
      - [customer_id, customer_id]
    via_attribute: status         # The attribute that carries the relationship value
    multiplicity: ManyToOne
    verbalizes:
      - "{Customer} has status {status}"
      - "Status {status} applies to {Customer}"
```

The `{status}` variable in `verbalizes` is resolved to the actual value of the `via_attribute` at query time.

```yaml
relationships:
  - name: Order.ordered_on
    from: Order
    to: Order
    join:
      - [order_id, order_id]
    via_attribute: event_date
    multiplicity: ManyToOne
    verbalizes:
      - "{Order} was ordered on {event_date}"
      - "On {event_date}, {Order} was placed"
```

### 3. Self-Referencing Relationships

When both `from` and `to` point to the same entity — used for attribute-based relationships or hierarchical structures.

```yaml
relationships:
  - name: OrderItem.has_quantity
    from: OrderItem
    to: OrderItem
    join:
      - [item_id, item_id]
    via_attribute: quantity
    multiplicity: ManyToOne
    verbalizes:
      - "{OrderItem} has quantity {quantity}"
```

## Multiplicity

| Multiplicity | Meaning | Example |
|-------------|---------|---------|
| `ManyToOne` | Many instances of `from` can relate to one instance of `to` | Many `Order`s placed by one `Customer` |
| `OneToOne` | Each `from` relates to exactly one `to` | One `Customer` has one `CustomerProfile` |

Only `ManyToOne` and `OneToOne` are currently supported.

## Verbalizes: Natural Language Templates

Every relationship MUST have at least one `verbalizes` entry. These are bidirectional natural-language sentences that describe the relationship. They are critical for AI understanding.

### Template Variables

| Variable | Resolves to |
|----------|------------|
| `{EntityName}` | The entity's display name |
| `{EntityName:roleName}` | The entity's name in a specific role (when the same entity appears multiple times) |
| `{attributeName}` | The value of a `via_attribute` (only in via-attribute relationships) |

### Template Examples

```yaml
# Standard entity-to-entity relationship
verbalizes:
  - "{Customer} places {Order}"
  - "{Order} is placed by {Customer}"

# Via-attribute relationship
verbalizes:
  - "{Customer} has status {status}"
  - "Status {status} applies to {Customer}"

# Time-based relationship
verbalizes:
  - "{Order} was ordered on {event_date}"
```

## JOIN Semantics

The `join` field maps columns from the `from` entity to the `to` entity. Both sides use the **concept** attribute name (not the physical column name), which is resolved through the entity's attribute definitions.

```yaml
# Example
join:
  - [customer_id, customer_id]    # from.Customer.customer_id = to.Order.customer_id

# Multi-column JOIN
join:
  - [customer_id, customer_id]
  - [order_date, event_date]      # from.Customer.order_date = to.Order.event_date
```

## Full Relationship Example

Here is the complete set of relationships from the sushi retail domain:

```yaml
relationships:
  # ─── Cross-entity relationships ───
  - name: Customer.places
    from: Customer
    to: Order
    join:
      - [customer_id, customer_id]
    multiplicity: ManyToOne
    verbalizes:
      - "{Customer} places {Order}"
      - "{Order} is placed by {Customer}"

  - name: Order.contains
    from: Order
    to: OrderItem
    join:
      - [order_id, order_id]
    multiplicity: ManyToOne
    verbalizes:
      - "{Order} contains {OrderItem}"
      - "{OrderItem} belongs to {Order}"

  - name: Order.served_by
    from: Order
    to: Waiter
    join:
      - [waiter_id, waiter_id]
    multiplicity: ManyToOne
    verbalizes:
      - "{Order} is served by {Waiter}"
      - "{Waiter} serves {Order}"

  # ─── Via-attribute relationships ───
  - name: Customer.has_status
    from: Customer
    to: Customer
    join:
      - [customer_id, customer_id]
    via_attribute: status
    multiplicity: ManyToOne
    verbalizes:
      - "{Customer} has status {status}"

  - name: Order.ordered_on
    from: Order
    to: Order
    join:
      - [order_id, order_id]
    via_attribute: event_date
    multiplicity: ManyToOne
    verbalizes:
      - "{Order} was ordered on {event_date}"

  - name: OrderItem.has_quantity
    from: OrderItem
    to: OrderItem
    join:
      - [item_id, item_id]
    via_attribute: quantity
    multiplicity: ManyToOne
    verbalizes:
      - "{OrderItem} has quantity {quantity}"
```

## Relationship Naming Convention

Use the pattern `SourceEntity.relationshipName`:

| Pattern | Example |
|---------|---------|
| Entity action | `Customer.places` → Order |
| Entity state | `Customer.has_status` |
| Entity containment | `Order.contains` → OrderItem |
| Entity serving/temporal | `Order.served_by` → Waiter, `Order.ordered_on` |

## Best Practices

1. **Always provide both forward and reverse `verbalizes`** — they enable AI to understand relationships from any direction
2. **Use `via_attribute` for dimensional attributes** — turn statuses, dates, and categories into queryable relationships
3. **Keep JOIN conditions minimal** — only include the columns needed to establish the relationship
4. **The relationship name is the full-qualified path** — `Customer.places`, not just `places`
5. **Cross-reference validation is automatic** — the loader checks that all referenced entities and attributes exist
