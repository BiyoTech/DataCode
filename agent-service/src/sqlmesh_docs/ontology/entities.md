# Entities

An **Entity** is the core building block of an ontology. It represents a business concept and simultaneously declares its physical mapping to a SQLMesh model — all in one place.

## Entity Schema

```yaml
entities:
  - name: <string>              # Business concept name (e.g. "Customer", "Order")
    description: <string>       # Human-readable description
    type: EntityType | ValueType# Concept type (see below)

    # ─── Physical Model Mapping ───
    model: <string>             # SQLMesh model FQN (e.g. "sushi.customers")

    # ─── Identity ───
    key:                        # Map of concept-level key to physical column
      <concept_attr>: <physical_column>
    # Example:
    #   customer_id: customer_id       # same name
    #   order_id: id                   # different name (grain: id AS order_id)

    # ─── Attributes (dimensions) ───
    attributes:
      - name: <string>          # Attribute name
        column: <string>        # Physical column name in the model
        type: categorical | time | numeric | text
        granularity: day        # Only when type=time
        description: <string>   # Optional
        ai_context: <AIContext> # Optional AI hints for this attribute

    # ─── Foreign Keys ───
    foreign_keys:
      - name: <string>          # FK name
        column: <string>        # Physical column
        references: <Concept.attribute>  # Dot notation: "Customer.customer_id"

    # ─── Concept Inheritance (OSI feature) ───
    sub_concepts:
      - name: <string>          # Sub-concept name
        derived_by: <string>    # "EXISTS(ParentEntity.places.ChildEntity)"
        description: <string>   # Optional
        ai_context: <AIContext> # Optional

    # ─── AI Context ───
    ai_context: <string | AIContext>  # Synonyms, examples, instructions
```

## Entity Types

### EntityType
An independent thing that can be uniquely identified and referenced. This is the default type for most business concepts.

| Example | Physical Model |
|---------|---------------|
| `Customer` | `sushi.customers` |
| `Order` | `sushi.orders` |
| `OrderItem` | `sushi.order_items` |
| `Waiter` | `sushi.waiter_names` |

### ValueType
A semantic data type that extends a built-in type. Used for reusable domain-specific types.

```yaml
entities:
  - name: Amount
    type: ValueType
    description: Monetary amount value type
    extends: [Decimal]
```

## Full Entity Example

```yaml
entities:
  - name: Customer
    description: A person or organization that places orders
    type: EntityType
    model: sushi.customers
    key:
      customer_id: customer_id
    attributes:
      - name: status
        column: status
        type: categorical
        ai_context:
          synonyms: [customer state, account status]
    sub_concepts:
      - name: ActiveCustomer
        derived_by: "EXISTS(Customer.places.Order)"
        description: A customer who has placed at least one order
    ai_context:
      synonyms: [client, account, patron]
```

## Entity with Renamed Key

When the concept-level name differs from the physical column name, use the key map:

```yaml
- name: Order
  model: sushi.orders
  key:
    order_id: id          # Concept: order_id, Physical column: id
```

## Entity with Foreign Keys

```yaml
- name: Order
  model: sushi.orders
  key:
    order_id: id
  foreign_keys:
    - name: customer_id
      column: customer_id
      references: Customer.customer_id    # Dot notation: Concept.attribute
    - name: waiter_id
      column: waiter_id
      references: Waiter.waiter_id
```

## Attribute Types

| Type | Use for | Example columns |
|------|---------|-----------------|
| `categorical` | Enum-like values | `status`, `category`, `type`, `gender` |
| `time` | Date/time columns | `event_date`, `created_at`, `timestamp` |
| `numeric` | Numbers | `amount`, `price`, `quantity`, `count` |
| `text` | Free-text columns | `name`, `description`, `notes` |

When `type: time`, you must also specify `granularity`:

```yaml
attributes:
  - name: event_date
    column: event_date
    type: time
    granularity: day       # One of: second, minute, hour, day, week, month, year
```

## Sub-Concepts (Concept Inheritance)

Sub-concepts define derived subsets of an entity without creating a new physical model. They are expressed in terms of relationships:

```yaml
sub_concepts:
  - name: ActiveCustomer
    derived_by: "EXISTS(Customer.places.Order)"
    description: A customer who has placed at least one order

  - name: VIPCustomer
    derived_by: "Customer.has_status.status = 'VIP'"
    description: A customer with VIP status
```

The `derived_by` value follows a simple expression grammar:
- `EXISTS(Parent.places.Child)` — parent must have at least one related child
- `Concept.relationship.attribute = 'value'` — attribute-based filtering

## Domain Model Hierarchy

```
EntityType (independent, identifiable things)
├── Customer
│   └── ActiveCustomer  (sub-concept)
├── Order
├── OrderItem
└── Waiter

ValueType (domain-specific data types)
└── Amount (extends Decimal)
```

## Best Practices

1. **Keep entity names singular and capitalized** — `Customer`, not `Customers`
2. **Always provide a `description`** — it helps both humans and AI understand the concept
3. **Add `ai_context.synonyms`** for common alternative names of the concept
4. **Use `sub_concepts` instead of creating separate entities** for derived subsets
5. **The `key` map is required** — it defines how the entity is uniquely identified
6. **Every `foreign_key.references` must point to an existing entity's key** — this is validated at load time
