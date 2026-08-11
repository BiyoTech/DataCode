# Ontology Query Guide

How to **consume** the business ontology (the semantic layer) and translate it into
correct analytical SQL. This is a READ-ONLY skill for querying data — it does NOT cover
authoring ontology files (see the `ontology_modeling` skill for that).

> **Goal**: given a natural-language question, resolve the relevant ontology entities,
> relationships, and metrics into a concrete SQL query over the real physical tables.

---

## Where the Ontology Lives

- Files: `{workspace}/ontology/*.yaml` — one YAML file per business domain.
- The file stem IS the ontology name (e.g. `sushi_retail.yaml` -> `sushi_retail`).
- Each file has three sections you translate into SQL: `entities`, `relationships`,
  `metrics` (plus optional `ai_context` hints).

Retrieve it with the read-only tools you already have:

1. `list_dir(path="ontology")` — see which domain ontologies exist.
2. `search_code(query=<key business terms>, file_pattern="ontology/**/*.yaml",
   output_mode="files_with_matches")` — find the matching file. User terms usually appear
   as `ai_context.synonyms`, `verbalizes`, or `description`.
3. `read_file` the matching YAML in full before writing SQL.

---

## Golden Rule

Concept-level names (entities, attributes) are NOT database identifiers. They ALWAYS
resolve to physical names via the entity's `model` and column maps. Never emit a concept
name into SQL — always emit the physical table / column it maps to.

---

## Translating Each Section

### entities — business concept -> physical table

- `model` is the physical SQLMesh model FQN -> use it as the table in `FROM` / `JOIN`.
- `key` maps each concept key to its physical column, e.g. `order_id: id` means the concept
  `Order.order_id` is physical column `id` in the model.
- `attributes[].column` maps a concept attribute to its physical column (with `type`:
  categorical / time / numeric / text). A `time` attribute is your date/period axis.
- `foreign_keys[].references` (`Concept.attribute`) tells you how entities connect.

### relationships — how to JOIN two entities

- `from` / `to` name the two entities; `join` is a list of `[from_attr, to_attr]` pairs
  given at the CONCEPT level. Resolve each side through the entity `key`/`attributes`
  column maps to build the physical `ON` clause.
- `multiplicity` (ManyToOne / OneToOne) tells you fan-out; aggregate carefully to avoid
  double counting across a ManyToOne join.
- `verbalizes` and `ai_context.synonyms` are how user phrasing maps to a relationship.

### metrics — the measure to compute (pick the pattern)

- **base**: `aggregate` (COUNT/SUM/AVG/MIN/MAX/COUNT_DISTINCT) + `expression`
  (`Concept.attribute`) + `entity` -> `SELECT <aggregate>(<physical_column>)` from that
  entity's model.
- **filtered**: base plus a `filter` (concept-level, e.g. `"Customer.status = 'ACTIVE'"`)
  and an `entities` list -> resolve the filter's concept columns to physical columns in
  `WHERE`, joining any extra entities via their relationships.
- **derived**: `derived.formula` is arithmetic over OTHER metric names -> expand each
  referenced metric to its aggregate expression, then combine (watch division-by-zero).
- **mapped**: `maps_to` names an existing SQLMesh `METRIC`; the aggregation lives in
  SQLMesh, so read that MODEL/METRIC (`search_code` / `list_models`) for the real logic
  rather than inventing it.

---

## Translation Checklist (before running a query)

- [ ] Every table in FROM/JOIN is a real `entity.model` FQN (confirm via `list_models` /
      `schema_discovery` if unsure).
- [ ] Every column is the PHYSICAL column from a `key` / `attributes[].column` map, not the
      concept name.
- [ ] Every JOIN `ON` comes from a relationship's `join` pairs; group-by dimension columns
      and time grain match an entity `time` / `categorical` attribute.
- [ ] The aggregation matches the metric's `aggregate` / `expression` (or the mapped SQLMesh
      METRIC).
- [ ] If a needed entity, metric, or column is NOT in the ontology, say so and fall back to
      knowledge / models / `schema_discovery` instead of guessing.

---

## Worked Example

Question: *"How many orders did active customers place per month?"*

1. `read_file` the domain ontology and locate:
   - Entity `Order` -> `model: sushi.orders`, `key: {order_id: id}`,
     attribute `event_date` (type time).
   - Entity `Customer` -> `model: sushi.customers`, `key: {customer_id: customer_id}`,
     attribute `status` (categorical).
   - Relationship `Customer.places` -> `from: Customer`, `to: Order`,
     `join: [[customer_id, customer_id]]`, `ManyToOne`.
   - Metric `total_orders` -> `aggregate: COUNT`, `expression: Order.order_id`.
2. Resolve to physical names and build SQL:

```sql
SELECT
    DATE_TRUNC('month', o.event_date) AS order_month,
    COUNT(o.id)                       AS total_orders
FROM sushi.orders     AS o
JOIN sushi.customers  AS c
  ON o.customer_id = c.customer_id       -- from relationship Customer.places
WHERE c.status = 'ACTIVE'                 -- concept Customer.status -> physical status
GROUP BY 1
ORDER BY 1
LIMIT 100;
```

Note how `Order.order_id` became `o.id`, `Customer.status` became `c.status`, and the JOIN
came straight from the relationship's `join` pairs.
