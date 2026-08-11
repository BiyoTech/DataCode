# Data Analysis Workflow

The full context-retrieval workflow for answering an analytical question with SQL. Load
this when you pick up a data question and want the concrete step-by-step tool sequence for
grounding the query in the workspace before writing SQL.

> **Principle**: retrieve grounding FIRST, write SQL LAST. The ontology is the primary
> source of truth; knowledge / ad-hocs / models supplement it.

---

## Workspace Layout (grounding sources, in priority order)

1. `ontology/**/*.yaml` — **business semantic layer (READ FIRST)**: entities (concept ->
   physical model + column mapping), relationships (JOIN conditions), metrics (aggregation
   logic). The authoritative bridge from business language to physical tables.
2. `knowledge/**/*` — business knowledge docs (metric definitions, glossary, calc rules).
3. `ad-hocs/**/*.sql` — previously saved ad-hoc queries (proven SQL patterns to reuse).
4. `models/**/*.sql` — SQLMesh model definitions (transformation logic & lineage).
5. `external_models/**/*.yaml` — external model definitions.

---

## Step-by-Step Workflow

### 1. Understand the question

Extract the key business terms:
- **entities** — nouns like "customer", "order", "product".
- **metrics** — measures like "revenue", "count", "average order value".
- **dimensions** — group-by / filter terms like "by month", "by region", "active".
- **time range** — "last 30 days", "Q3", "year over year".

### 2. Retrieve the ontology FIRST

```
list_dir(path="ontology")
search_code(query="<key business terms>", file_pattern="ontology/**/*.yaml",
            output_mode="files_with_matches")
read_file(path="ontology/<matched_domain>.yaml")
```

Then map user terms to concrete entities, relationships, and metrics, and resolve them to
physical models and columns. For the translation rules, load the `ontology_query` skill.

### 3. Supplement context only as needed

Do this when the ontology is missing, incomplete, or the question goes beyond it:

```
search_code(query="<key terms>", file_pattern="knowledge/**/*.md")   # extra business rules
search_code(query="<key terms>", file_pattern="ad-hocs/**/*.sql")    # reuse proven SQL
read_file(path="ad-hocs/<best_match>.sql")
search_code(query="<key terms>", file_pattern="models/**/*.sql")     # how models are built
list_models()
get_lineage(model="<fqn>")
schema_discovery(...)                                                # confirm real columns/types
```

If a step yields nothing relevant, note it briefly and continue — do not block on it.

### 4. Write and execute SQL

Use `query` with the semantics resolved from the ontology. Always add a `LIMIT` to
exploratory queries (default 100 rows). Prefer `schema_discovery` before querying tables
whose columns you have not confirmed.

### 5. Present findings

Answer in Markdown, conclusion first. For output formatting and charting, load the
`analysis_reporting` skill.

---

## Related Skills

- **ontology_query** — how to translate ontology entities/relationships/metrics into SQL.
- **analysis_reporting** — Markdown + ECharts output rules.
- **sqlmesh_reference** — SQL dialect / model kind specifics when needed.

---

## Guardrails

- You are READ-FIRST: you analyze data but do NOT create/modify SQLMesh models or ontology
  files, do NOT run SQLMesh CLI (plan/test/audit/apply), and do NOT edit project files.
- When the user asks to modify models, ontology, or run deployment commands, hand off to the
  Orchestrator (triage) with clear context about what is needed.
