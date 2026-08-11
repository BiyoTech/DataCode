# Ontology Service

Semantic layer core library for the AgenticData platform. Provides structured,
on-demand retrieval of business concepts (entities, relationships, metrics)
defined in per-workspace ontology YAML files, enabling AI agents to generate
accurate SQL without一次性 loading the full context.

## Quick Start

```bash
# Install in development mode
cd ontology-service
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Lint
ruff check src/ tests/
```

## Ontology File Format

Each SQLMesh workspace has an `ontology/` directory. Every `.yaml` file inside
defines one business domain. **The file name (without extension) is the
ontology name** — the `name` field is not written inside the YAML.

```
{workspace}/
└── ontology/
    ├── sushi_retail.yaml      # ontology name = "sushi_retail"
    └── inventory.yaml         # ontology name = "inventory"
```

Minimal example:

```yaml
version: "0.1.0"
description: Sushi restaurant retail analytics
ai_context:
  instructions: "Use for sushi restaurant analytics"
  synonyms: [sushi analytics, restaurant data]

entities:
  - name: Customer
    type: EntityType
    model: sushi.customers
    key: { customer_id: customer_id }
    # ...

relationships:
  - name: Customer.places
    from: Customer
    to: Order
    join: [[customer_id, customer_id]]
    multiplicity: ManyToOne
    verbalizes:
      - "{Customer} places {Order}"

metrics:
  - name: total_orders
    aggregate: COUNT
    expression: Order.order_id
    entity: Order
```

## API Overview

### ModelRegistry — auto-discovery

```python
from pathlib import Path
from src.loader import ModelRegistry

registry = ModelRegistry(Path("/path/to/sushi_workspace"))

# List all ontologies (lightweight, no full parse)
for m in registry.list_models():
    print(m.name, m.description)

# Lazy-load a full semantic model
model = registry.get_model("sushi_retail")
```

### SemanticSearchService — agent retrieval

```python
from src.semantic_search import SemanticSearchService

search = SemanticSearchService(registry)

# Step 1: search by keyword
entities = search.search_entities("sushi_retail", "active customer")
metrics = search.search_metrics("sushi_retail", "order count")

# Step 2: get full details
detail = search.get_entity_detail("sushi_retail", "Customer")
graph = search.get_entity_graph("sushi_retail", "Customer", depth=2)
```

### CrossRefValidator — reference integrity

```python
from src.cross_ref_validator import CrossRefValidator

errors = CrossRefValidator.validate(model)
if errors:
    for e in errors:
        print(f"[{e.severity}] {e.path}: {e.message}")
```

## Architecture

```
src/
├── models.py               # Pydantic v2 domain + return types
├── loader.py               # YAML loading + ModelRegistry (auto-discovery)
├── cross_ref_validator.py  # Cross-reference validation
└── semantic_search.py      # 6-method retrieval service (core)
```

See `doc/semantic-layer-design.md` for the full design specification.
