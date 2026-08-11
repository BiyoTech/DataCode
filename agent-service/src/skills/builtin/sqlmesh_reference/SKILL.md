# SQLMesh Reference

Complete SQLMesh documentation index. This skill provides a structured map of all available
SQLMesh documentation files. After loading this skill, use `read_file` to access any
specific document by its path (paths are relative to `src/sqlmesh_docs/`).

## How to Use This Skill

1. Scan the topic table below to find relevant documentation areas
2. Use `read_file` with the full path to read a specific document
3. Documents are at: `src/sqlmesh_docs/<topic>/<file>.md`

---

## Concepts — Core SQLMesh Ideas

| File | Description |
|------|-------------|
| `concepts/overview.md` | High-level SQLMesh architecture and philosophy |
| `concepts/glossary.md` | Terminology reference |
| `concepts/models/` | Model definitions, model kinds (FULL, INCREMENTAL_BY_TIME_RANGE, SCD_TYPE_2, VIEW, SEED, etc.) |
| `concepts/models/model_kinds.md` | **Complete model kind reference** — parameters, use cases, examples |
| `concepts/macros/` | Jinja macros and SQLMesh built-in macros |
| `concepts/architecture/` | Internal architecture details |
| `concepts/metrics/` | Metric definitions |
| `concepts/audits.md` | Data quality audits |
| `concepts/tests.md` | Unit tests for models |
| `concepts/environments.md` | Virtual environments concept |
| `concepts/plans.md` | Plan lifecycle and operations |
| `concepts/state.md` | State management and tracking |

---

## Guides — How-to Walkthroughs

| File | Description |
|------|-------------|
| `guides/models.md` | Creating and working with models |
| `guides/configuration.md` | Global and project-level configuration |
| `guides/connections.md` | Database connection setup |
| `guides/projects.md` | Project structure and setup |
| `guides/incremental_time.md` | Time-based incremental loading patterns |
| `guides/model_selection.md` | Selecting subsets of models for operations |
| `guides/testing.md` | Writing and running tests |
| `guides/scheduling.md` | Scheduling and automation |
| `guides/notifications.md` | Alert and notification setup |
| `guides/custom_materializations.md` | Custom materialization logic |
| `guides/customizing_sqlmesh.md` | Extending SQLMesh behavior |
| `guides/linter.md` | SQL linter configuration |
| `guides/migrations.md` | Version migrations |
| `guides/table_migration.md` | Table schema migration |
| `guides/tablediff.md` | Comparing tables between environments |
| `guides/signals.md` | Signal-based triggers |
| `guides/multi_engine.md` | Multi-engine projects |
| `guides/multi_repo.md` | Multi-repository setups |
| `guides/isolated_systems.md` | Isolated system patterns |

---

## Integrations — Third-Party Connectors

| File | Description |
|------|-------------|
| `integrations/overview.md` | Integration overview |
| `integrations/dbt.md` | dbt project migration |
| `integrations/dlt.md` | dlt data loader integration |
| `integrations/github.md` | GitHub CI/CD integration |
| `integrations/engines/` | **17 engine adapters** (see below) |

### Engine Adapters (integrations/engines/)

| File | Engine |
|------|--------|
| `integrations/engines/bigquery.md` | BigQuery |
| `integrations/engines/snowflake.md` | Snowflake |
| `integrations/engines/redshift.md` | Redshift |
| `integrations/engines/databricks.md` | Databricks |
| `integrations/engines/duckdb.md` | DuckDB |
| `integrations/engines/postgres.md` | PostgreSQL |
| `integrations/engines/mysql.md` | MySQL |
| `integrations/engines/trino.md` | Trino |
| `integrations/engines/spark.md` | Spark |
| `integrations/engines/clickhouse.md` | ClickHouse |
| `integrations/engines/athena.md` | Athena |
| `integrations/engines/azuresql.md` | Azure SQL |
| `integrations/engines/fabric.md` | Microsoft Fabric |
| `integrations/engines/mssql.md` | SQL Server |
| `integrations/engines/motherduck.md` | MotherDuck |
| `integrations/engines/gcp-postgres.md` | GCP Cloud SQL PostgreSQL |
| `integrations/engines/risingwave.md` | RisingWave |

---

## Reference — CLI, Config, API

| File | Description |
|------|-------------|
| `reference/cli.md` | **Complete CLI command reference** |
| `reference/configuration.md` | Global configuration reference |
| `reference/model_configuration.md` | Per-model configuration options |
| `reference/overview.md` | Reference overview |
| `reference/notebook.md` | Notebook interface |
| `reference/python.md` | Python API reference |

---

## Ontology — Semantic Layer

The ontology semantic layer enriches SQLMesh physical models with business meaning, relationships, and AI-hints. Ontologies are authored as YAML files under `ontology/` and can be searched by AI Agents at query time.

| File | Description |
|------|-------------|
| `ontology/overview.md` | **What is the ontology?** — architecture, file structure, AI Agent workflow |
| `ontology/entities.md` | **Entity definition reference** — business concepts, physical model mapping, attributes, foreign keys, sub-concepts |
| `ontology/relationships.md` | **Relationship definition reference** — cross-entity, via-attribute, and self-referencing relationships with JOIN conditions and natural-language verbalizations |
| `ontology/metrics.md` | **Metric definition reference** — four metric patterns: base, filtered, derived, and mapped (SQLMesh METRIC) |
| `ontology/ai_context.md` | **AI Context annotations** — structured hints (synonyms, instructions, examples) for AI understanding at every ontology level |

---

## FAQ

| File | Description |
|------|-------------|
| `faq/faq.md` | Frequently asked questions |

---

## Quick Lookup Guide

| Need | Read |
|------|------|
| Which model kind to choose? | `concepts/models/model_kinds.md` |
| How to write a model? | `guides/models.md` + `concepts/models/model_kinds.md` |
| CLI command syntax? | `reference/cli.md` |
| Engine connection config? | `integrations/engines/<engine>.md` + `guides/connections.md` |
| Model configuration options? | `reference/model_configuration.md` |
| How to write tests? | `concepts/tests.md` + `guides/testing.md` |
| How to add audits? | `concepts/audits.md` |
| Project setup? | `guides/projects.md` |
| Macro syntax? | `concepts/macros/` |
| JavaScript macros? | `concepts/macros/` (sub-files) |
| What is the ontology semantic layer? | `ontology/overview.md` |
| How to define entities? | `ontology/entities.md` |
| How to define relationships and JOINs? | `ontology/relationships.md` |
| How to define metrics? | `ontology/metrics.md` |
| How to add AI hints (synonyms, examples)? | `ontology/ai_context.md` |
