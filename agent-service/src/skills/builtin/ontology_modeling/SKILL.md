# Ontology Modeling Guide

Step-by-step guide for authoring the **semantic layer** — business ontology YAML
files that map SQLMesh physical models to business concepts, relationships, and
metrics. This skill covers the full workflow: discovering the underlying models,
writing the ontology definition, and validating it.

> **Goal**: guide the creation and modification of ontology files under
> `{workspace}/ontology/`. Each file describes ONE business domain: its
> entities, relationships, metrics, and AI hints.

---

## What Is an Ontology?

The ontology is a **decoupled semantic layer** on top of SQLMesh. It never
modifies existing `MODEL` / `METRIC` definitions — it only enriches them with
business meaning so AI agents and users can reason about the data in natural
language.

- **File location**: `{workspace}/ontology/<name>.yaml`
- **One file = one domain** (e.g. `sushi_retail.yaml`, `finance.yaml`)
- **The file name IS the ontology name** — do NOT write a `name:` field inside
  the YAML; it is injected automatically at load time.

Full reference docs live under `src/sqlmesh_docs/ontology/`:

| File | Read when you need to define... |
|------|--------------------------------|
| `ontology/overview.md` | Architecture, file structure, the AI-agent workflow |
| `ontology/entities.md` | Entities, physical model mapping, attributes, keys, foreign keys, sub-concepts |
| `ontology/relationships.md` | Cross-entity / via-attribute / self relationships + JOIN conditions + verbalizes |
| `ontology/metrics.md` | The four metric patterns: base, filtered, derived, mapped |
| `ontology/ai_context.md` | Synonyms, instructions, examples for AI understanding |

Use `read_file` to open any of these before writing non-trivial definitions.

---

## Standard Workflow

Always follow this three-phase workflow. Call `write_todo` first for any
multi-entity ontology so progress stays visible.

### Phase 1 — Discover the underlying models

An ontology can only reference **models that actually exist**. Before writing
anything, find the SQLMesh models (and external models) that back the requested
business concepts.

1. **List existing models**: use `list_models` to enumerate the project's
   SQLMesh models and their fully-qualified names (e.g. `sushi.customers`),
   noting each model's **gateway** (external models are bound to a specific
   gateway; base models have none).
2. **Search by keyword**: use `search_code` to locate model definitions related
   to the user's domain, searching both:
   - `models/**` — SQLMesh-managed models (`.sql` / `.py`)
   - `external_models/**` — imported external table schemas (`.yaml`)
3. **Inspect schemas**: use `read_file` on the matched model files (and
   `schema_discovery` when available) to learn the real column names, types,
   grain (primary key), and foreign-key columns. These physical column names are
   what the ontology's `key`, `attributes[].column`, and `join` fields must map
   to.
4. Confirm each intended entity has a real backing model FQN and each intended
   foreign key / JOIN column exists. Never invent a column or model name.
5. **Confirm a single gateway**: all entities of one ontology must come from the
   **same gateway**. If the requested concepts span two gateways, split them
   into separate ontology files (one per gateway). Record the target gateway —
   it becomes the top-level `gateway` field in Phase 2.

> **Why this matters**: `Entity.model` must be a real SQLMesh model FQN and
> `Metric.maps_to` must be a real SQLMesh `METRIC`. Validation (Phase 3) checks
> these against the live workspace and fails on typos or missing objects, and
> also fails when an ontology mixes models from multiple gateways.

### Phase 2 — Author the ontology YAML

1. Decide the ontology file name from the domain (e.g. `sushi_retail.yaml`) and
   place it at `{workspace}/ontology/<name>.yaml`.
2. Build the five top-level sections, consulting the reference docs as needed:

   ```yaml
   version: "0.1.0"
   gateway: postgres                 # SQLMesh gateway all entity models belong to
   description: >
     Business-friendly description of this domain.
   ai_context:                       # optional global AI hints
     instructions: "Use this model for ..."
     synonyms: [domain synonym, ...]

   entities: [...]                   # business concepts mapped to SQLMesh models
   relationships: [...]              # semantic relationships + JOIN conditions
   metrics: [...]                    # business metrics + aggregation logic
   ```

3. **Entities** (`ontology/entities.md`): each entity declares its business
   `name`, `type` (`EntityType` / `ValueType`), physical `model` FQN, a `key`
   map (concept attr -> physical column), `attributes`, `foreign_keys`
   (`references: Concept.attribute`), and optional `sub_concepts`.
4. **Relationships** (`ontology/relationships.md`): declare `from` / `to`
   entities, the `join` column pairs (using **concept** attribute names),
   `multiplicity` (`ManyToOne` / `OneToOne`), and REQUIRED bidirectional
   `verbalizes` natural-language templates.
5. **Metrics** (`ontology/metrics.md`): pick the right pattern —
   - base (`aggregate` + `expression` + `entity`),
   - filtered (adds `filter` + `entities`),
   - derived (`derived.formula` over other metric names),
   - mapped (`maps_to` an existing SQLMesh `METRIC`).
6. **AI context** (`ontology/ai_context.md`): add `synonyms`, `instructions`,
   and `examples` at every useful level — they drive semantic search quality.
7. Use `create_file` for a brand-new ontology file; use `apply_patch` to modify
   an existing one (always `read_file` first so patch context matches).

### Phase 3 — Validate

After writing or modifying the file, ALWAYS validate it via the `shell` tool:

```
shell(commands=["sqlmesh ontology validate <name>"])
```

- `sqlmesh ontology validate <name>` — validate a single ontology (by file stem).
- `sqlmesh ontology validate --all` — validate every ontology in the workspace.
- `sqlmesh ontology list` — list all available ontologies.

Validation runs cross-reference and SQLMesh existence checks, including:
- `foreign_key.references`, relationship `from`/`to`/`join`, metric
  `expression`/`filter`/`derived.formula` point to existing entities/attributes.
- `entity.extends` / `identify_by`, and `derived_by` / `requires` path roots.
- `Entity.model` maps to a real SQLMesh model, and `Metric.maps_to` maps to a
  real SQLMesh `METRIC` (when the workspace runtime is reachable).
- **Gateway consistency**: every `Entity.model` resolves to the ontology's
  declared `gateway`, and no ontology mixes models from multiple gateways.

**If validation reports errors**, read each `[severity] path: message` line,
fix the offending field in the YAML (re-checking the real model schema from
Phase 1 when a mapping is wrong), and re-run validation until it reports VALID.
Do not consider the task complete while validation still fails.

---

## Important Rules

1. **Discover before authoring** — never reference a model, column, or metric you
   have not confirmed exists in Phase 1.
2. **File name = ontology name** — never add a `name:` field inside the YAML.
3. **One file per business domain** — keep `finance` and `marketing` separate.
4. **Single gateway per ontology** — declare a top-level `gateway` and reference
   models from that gateway only. Never mix models from two gateways in one
   ontology; split them into separate files instead. Different ontologies MAY
   reuse the same gateway.
4. **Ontology is decoupled** — files live only under `ontology/`; never edit
   `models/` or `METRIC` definitions from this skill.
5. **`key` maps are required** — they define how each entity is identified.
6. **Every `foreign_key.references` and JOIN column must resolve** — validation
   enforces this.
7. **Relationships MUST have `verbalizes`** — provide both forward and reverse
   natural-language templates.
8. **Prefer `maps_to`** when a SQLMesh `METRIC` already exists — add only AI
   context rather than redefining the aggregation.
9. **Rich `ai_context`** — add 3-5 synonyms per entity plus example questions;
   they directly improve semantic search matching.
10. **Always validate after writing** — a change is not done until
    `sqlmesh ontology validate <name>` reports VALID.

---

## Quick Reference — Minimal Ontology

```yaml
version: "0.1.0"
gateway: postgres                   # all entity models below belong to this gateway
description: Sushi restaurant retail analytics semantic model
ai_context:
  instructions: "Use this model for sushi sales and customer analytics"
  synonyms: [sushi analytics, restaurant data]

entities:
  - name: Customer
    type: EntityType
    model: sushi.customers          # real SQLMesh model FQN
    key:
      customer_id: customer_id
    attributes:
      - name: status
        column: status
        type: categorical
    ai_context:
      synonyms: [client, account, patron]

  - name: Order
    type: EntityType
    model: sushi.orders
    key:
      order_id: id                  # concept order_id -> physical column id
    foreign_keys:
      - name: customer_id
        column: customer_id
        references: Customer.customer_id

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

metrics:
  - name: total_orders
    description: Total number of orders
    aggregate: COUNT
    expression: Order.order_id
    entity: Order
    ai_context:
      synonyms: [order count, number of orders]
```

Then: `shell(commands=["sqlmesh ontology validate sushi_retail"])`
