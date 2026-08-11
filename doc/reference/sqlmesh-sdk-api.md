# SQLMesh Python SDK API Reference

> **Purpose**: This document provides a comprehensive reference for using the SQLMesh Python SDK programmatically,
> replacing CLI-based workflows with SDK calls for automation, orchestration, and integration scenarios.
>
> **Source**: https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh.html
> **Version**: Based on SQLMesh stable (as of 2026-06)

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Top-Level API Exports](#2-top-level-api-exports)
3. [Context — The Core Entry Point](#3-context--the-core-entry-point)
4. [Model Definitions](#4-model-definitions)
5. [Configuration](#5-configuration)
6. [EngineAdapter — Database Operations](#6-engineadapter--database-operations)
7. [Macros](#7-macros)
8. [Snapshots](#8-snapshots)
9. [Plan & PlanBuilder](#9-plan--planbuilder)
10. [Logging & Utilities](#10-logging--utilities)
11. [Common SDK Patterns](#11-common-sdk-patterns)

---

## 1. Quick Start

### Installation

```bash
pip install sqlmesh
```

### Minimal SDK Usage

```python
from sqlmesh.core.context import Context

# Create a context pointing to a SQLMesh project directory
context = Context(paths="/path/to/project")

# Create a plan against staging and apply it
plan = context.plan("staging")
context.apply(plan)

# Run audits
context.audit("yesterday", "now")

# Run model tests
context.test()
```

### Programmatic Context with Inline Config

```python
from sqlmesh.core.context import Context
from sqlmesh.core.config import Config
from sqlmesh.core.config.connection import DuckDBConnectionConfig
from sqlmesh.core.config.gateway import GatewayConfig

config = Config(
    gateways={
        "local": GatewayConfig(connection=DuckDBConnectionConfig()),
    },
    default_gateway="local",
)

context = Context(paths="/path/to/project", config=config)
```

---

## 2. Top-Level API Exports

These are the primary objects available via `import sqlmesh` or direct sub-module imports:

| Object | Source Module | Type | Description |
|--------|-------------|------|-------------|
| `Config` | `sqlmesh.core.config` | Class | Main project configuration |
| `Context` | `sqlmesh.core.context` | Class | Core entry point for all SDK operations |
| `ExecutionContext` | `sqlmesh.core.context` | Class | Minimal context for model execution |
| `EngineAdapter` | `sqlmesh.core.engine_adapter` | Class | Database interaction layer |
| `SQL` | `sqlmesh.core.macros` | NewType | Type hint for SQL string parameters in macros |
| `macro` | `sqlmesh.core.macros` | Decorator | Register a function as a macro |
| `Model` | `sqlmesh.core.model` | Class | Base model class |
| `model` | `sqlmesh.core.model` | Decorator | Define a model programmatically |
| `signal` | `sqlmesh.core.signal` | Decorator | Define a signal |
| `Snapshot` | `sqlmesh.core.snapshot` | Class | Versioned snapshot of a model |
| `CustomMaterialization` | `sqlmesh.core.snapshot.evaluator` | Class | Base class for custom materializations |
| `CustomKind` | `sqlmesh.core.model.kind` | Class | Custom model kind |
| `debug_mode_enabled` | `sqlmesh.utils` | Function | Check if debug mode is active |
| `enable_debug_mode` | `sqlmesh.utils` | Function | Enable debug mode |
| `str_to_bool` | `sqlmesh.utils` | Function | Convert string to boolean |
| `DatetimeRanges` | `sqlmesh.utils.date` | Class | Date/time range utilities |

---

## 3. Context — The Core Entry Point

`Context` encapsulates a SQLMesh environment. It discovers and loads models, macros, and audits,
then provides methods for planning, running, auditing, and more.

### Constructor

```python
Context(
    paths: Union[str | Path, Iterable[str | Path]] = "",
    config: Optional[Union[Config, str, Dict[Path, Config]]] = None,
    gateway: Optional[str] = None,
    concurrent_tasks: Optional[int] = None,
    loader: Optional[Type[Loader]] = None,
    load: bool = True,
    users: Optional[List[User]] = None,
    state_sync: Optional[StateSync] = None,
    notification_targets: Optional[List[NotificationTarget]] = None,
    config_loader_kwargs: Optional[Dict[str, Any]] = None,
    selector: Optional[Type[Selector]] = None,
)
```

| Parameter | Description |
|-----------|-------------|
| `paths` | Directory containing SQLMesh project files |
| `config` | A `Config` object, name of config in `config.py`, or dict of `{Path: Config}` |
| `gateway` | Name of the gateway to use (from `Config.gateways`) |
| `concurrent_tasks` | Max concurrent tasks using the connection |
| `load` | Whether to auto-load all models/macros on init (default `True`) |
| `users` | List of users known to SQLMesh |

### Key Methods

#### Plan & Apply

```python
# Create an interactive plan
plan = context.plan(
    environment: Optional[str] = None,        # Target environment (default: prod)
    *,
    start: Optional[TimeLike] = None,          # Backfill start date
    end: Optional[TimeLike] = None,            # Backfill end date
    execution_time: Optional[TimeLike] = None, # Execution time reference
    create_from: Optional[str] = None,         # Environment to create from
    skip_tests: Optional[bool] = None,         # Skip unit tests
    restate_models: Optional[Iterable[str]] = None,  # Models to restate
    no_gaps: Optional[bool] = None,            # Ensure no data gaps
    skip_backfill: Optional[bool] = None,      # Skip backfill step
    empty_backfill: Optional[bool] = None,     # Record intervals without data
    forward_only: Optional[bool] = None,       # Forward-only changes
    allow_destructive_models: Optional[Collection[str]] = None,
    allow_additive_models: Optional[Collection[str]] = None,
    no_prompts: Optional[bool] = None,         # Disable interactive prompts
    auto_apply: Optional[bool] = None,         # Auto-apply after creation
    no_auto_categorization: Optional[bool] = None,
    effective_from: Optional[TimeLike] = None,
    include_unmodified: Optional[bool] = None,
    select_models: Optional[Collection[str]] = None,  # Model selection filter
    backfill_models: Optional[Collection[str]] = None,
    categorizer_config: Optional[CategorizerConfig] = None,
    enable_preview: Optional[bool] = None,
    no_diff: Optional[bool] = None,            # Hide text diff
    run: Optional[bool] = None,                # Run latest intervals
    diff_rendered: Optional[bool] = None,
    skip_linter: Optional[bool] = None,
    explain: Optional[bool] = None,
    ignore_cron: Optional[bool] = None,
    min_intervals: Optional[int] = None,
) -> Plan

# Apply a plan
context.apply(plan)
```

```python
# Create a PlanBuilder for more control
builder = context.plan_builder(environment="staging", skip_tests=True)
plan = builder.build()
context.apply(plan)
```

#### Run

```python
# Run the entire DAG
status = context.run(
    environment: Optional[str] = None,
    *,
    start: Optional[TimeLike] = None,
    end: Optional[TimeLike] = None,
    execution_time: Optional[TimeLike] = None,
    skip_janitor: bool = False,
    ignore_cron: bool = False,
    select_models: Optional[Collection[str]] = None,
    exit_on_env_update: Optional[int] = None,
    no_auto_upstream: bool = False,
) -> CompletionStatus
```

#### Evaluate & Render

```python
# Evaluate a model (run query against DB, no side effects)
df = context.evaluate(
    model_or_snapshot: ModelOrSnapshot,
    start: TimeLike,
    end: TimeLike,
    execution_time: TimeLike,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> DF  # Pandas or PySpark DataFrame

# Render a model's query (expand macros, optionally expand referenced models)
expr = context.render(
    model_or_snapshot: ModelOrSnapshot,
    *,
    start: Optional[TimeLike] = None,
    end: Optional[TimeLike] = None,
    execution_time: Optional[TimeLike] = None,
    expand: Union[bool, Iterable[str]] = False,
    **kwargs: Any,
) -> exp.Expression
```

#### Query Data

```python
# Fetch DataFrame from SQL
df = context.fetchdf(
    query: Union[exp.Expression, str],
    quote_identifiers: bool = False,
) -> pd.DataFrame

# Fetch PySpark DataFrame
df = context.fetch_pyspark_df(
    query: Union[exp.Expression, str],
    quote_identifiers: bool = False,
) -> PySparkDataFrame

# Resolve a model name to its physical table name
table_name = context.resolve_table(model_name: str) -> str
```

#### Model Management

```python
# Get a model by name
model = context.get_model(
    model_or_snapshot: ModelOrSnapshot,
    raise_if_missing: bool = False,
) -> Optional[Model]

# Get a snapshot by name
snapshot = context.get_snapshot(
    node_or_snapshot: NodeOrSnapshot,
    raise_if_missing: bool = False,
) -> Optional[Snapshot]

# Upsert (update or insert) a model
model = context.upsert_model(
    model: Union[str, Model],
    **kwargs: Any,
) -> Model
```

#### Properties

```python
# All registered models
context.models      # -> MappingProxyType[str, Model]

# All registered metrics
context.metrics     # -> MappingProxyType[str, Metric]

# All standalone audits
context.standalone_audits  # -> MappingProxyType[str, StandaloneAudit]

# Snapshots based on current models
context.snapshots   # -> Dict[str, Snapshot]

# DAG of model dependencies
context.dag         # -> DAG[str]

# Default engine adapter
context.engine_adapter  # -> EngineAdapter

# State sync (for reading/writing persisted state)
context.state_sync     # -> StateSync
context.state_reader   # -> StateReader

# Default dialect
context.default_dialect  # -> Optional[str]

# Default catalog
context.default_catalog  # -> Optional[str]
```

#### Other Methods

```python
# Refresh models (reload if files changed)
context.refresh()

# Force reload all models
context.load(update_schemas: bool = True) -> Context

# Format all SQL models
context.format(
    transpile: Optional[str] = None,
    rewrite_casts: Optional[bool] = None,
    append_newline: Optional[bool] = None,
    *,
    check: Optional[bool] = None,
    paths: Optional[Tuple[Union[str, Path], ...]] = None,
) -> bool

# Run audits
context.audit(start: TimeLike, end: TimeLike)

# Run model tests
context.test()

# Destroy all environments and resources
context.destroy() -> bool

# Get scheduler
scheduler = context.scheduler(environment: Optional[str] = None) -> Scheduler

# Get execution context
exec_ctx = context.execution_context(
    deployability_index: Optional[DeployabilityIndex] = None,
    engine_adapter: Optional[EngineAdapter] = None,
    snapshots: Optional[Dict[str, Snapshot]] = None,
) -> ExecutionContext
```

---

## 4. Model Definitions

### Model Types

| Class | Description |
|-------|-------------|
| `Model` | Base model class (abstract) |
| `SqlModel` | SQL-based model (query-driven) |
| `PythonModel` | Python-based model (function-driven) |
| `SeedModel` | Static data (CSV) model |
| `ExternalModel` | External data source model |

### Model Kinds

| Kind | Description |
|------|-------------|
| `FullKind` | Full refresh on every run |
| `IncrementalByTimeRangeKind` | Incremental by time column |
| `IncrementalByUniqueKeyKind` | Incremental by unique key |
| `IncrementalByPartitionKind` | Incremental by partition |
| `IncrementalUnmanagedKind` | Unmanaged incremental |
| `SCDType2ByTimeKind` | Slowly Changing Dimension Type 2 (by time) |
| `SCDType2ByColumnKind` | SCD Type 2 (by column) |
| `ViewKind` | Virtual view |
| `EmbeddedKind` | Embedded (inlined) model |
| `SeedKind` | Seed (static CSV) |
| `CustomKind` | Custom materialization |
| `ManagedKind` | Engine-managed model |

### Model Parameters (via `ModelMeta`)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | required | Model name (`[catalog].[db].table`) |
| `dialect` | `Optional[str]` | `None` | SQL dialect for the model |
| `owner` | `Optional[str]` | `None` | Model owner |
| `cron` | `str` | `'@daily'` | Refresh schedule (cron expression) |
| `description` | `Optional[str]` | `None` | Model description |
| `stamp` | `Optional[str]` | `None` | Arbitrary string to force new version |
| `start` | `Optional[TimeLike]` | `None` | Earliest backfill date |
| `end` | `Optional[TimeLike]` | `None` | Latest backfill date |
| `lookback` | `int` | `0` | Lookback intervals for incremental models |
| `table_format` | `Optional[str]` | `None` | Table format (e.g., `iceberg`, `delta`) |
| `storage_format` | `Optional[str]` | `None` | Storage format (e.g., `parquet`, `orc`) |
| `partitioned_by` | `Optional[List]` | `None` | Partition columns/expressions |
| `clustered_by` | `Optional[List]` | `None` | Cluster columns/expressions |
| `tags` | `Optional[List[str]]` | `None` | Tags for organization |
| `grains` | `Optional[List]` | `None` | Grain definitions |
| `references` | `Optional[List]` | `None` | Reference definitions |
| `enabled` | `bool` | `True` | Whether the model is active |
| `physical_schema_override` | `Optional[str]` | `None` | Override physical schema name |
| `audits` | `Optional[List]` | `None` | Audit configurations |
| `columns` | `Optional[Dict]` | `None` | Column type definitions |
| `session_properties` | `Optional[Dict]` | `None` | Session-level properties |
| `physical_properties` | `Optional[Dict]` | `None` | Physical table properties |
| `virtual_properties` | `Optional[Dict]` | `None` | Virtual view properties |
| `gateway` | `Optional[str]` | `None` | Specific gateway name |
| `retention` | `Optional[int]` | `None` | Data retention window (seconds) |
| `batch_size` | `Optional[int]` | `None` | Batch size for interval processing |
| `batch_concurrency` | `Optional[int]` | `None` | Max concurrent batch evaluations |
| `allow_partials` | `bool` | `False` | Allow partial interval evaluation |
| `grants` | `Optional[Dict]` | `None` | Permission grants |

### Factory Functions

```python
from sqlmesh.core.model import (
    create_sql_model,
    create_python_model,
    create_seed_model,
    create_external_model,
)

# Create a SQL model programmatically
sql_model = create_sql_model(
    name="db.my_model",
    query="SELECT id, name FROM source_table",
    kind=IncrementalByTimeRangeKind(time_column="created_at"),
    cron="@daily",
    start="2024-01-01",
)

# Create an external model
ext_model = create_external_model(
    name="raw.external_data",
    columns={"id": "INT", "value": "VARCHAR"},
)
```

### `model` Decorator

```python
from sqlmesh.core.model import model, IncrementalByTimeRangeKind

@model(
    name="db.orders_daily",
    kind=IncrementalByTimeRangeKind(time_column="order_date"),
    cron="@daily",
    start="2024-01-01",
    partitioned_by=["order_date"],
    tags=["daily", "orders"],
)
def execute(context, start, end, execution_time, **kwargs):
    """Python model that returns a DataFrame."""
    import pandas as pd
    # Query upstream and return a DataFrame
    df = context.fetchdf("SELECT * FROM db.raw_orders WHERE order_date BETWEEN ...")
    return df
```

### Model Key Methods

```python
# Render the model's query
query = model.render_query(
    start=None, end=None, execution_time=None,
    snapshots=None, table_mapping=None,
    expand=tuple(), deployability_index=None,
    engine_adapter=None, **kwargs,
) -> Optional[exp.Query]

# Render query or raise
query = model.render_query_or_raise(...)  # Same params, raises on failure

# Render model definition as SQL expressions
expressions = model.render_definition(
    include_python=True,
    include_defaults=False,
    render_query=False,
) -> List[exp.Expression]

# Check if breaking change
result = model.is_breaking_change(previous_model) -> Optional[bool]

# Get column types
cols = model.columns_to_types  # -> Optional[Dict[str, exp.DataType]]

# Get dependencies
deps = model.depends_on  # -> Set[str]

# Text diff
diff_str = model.text_diff(other_model, rendered=False) -> str
```

---

## 5. Configuration

### Config Class

```python
from sqlmesh.core.config import Config
from sqlmesh.core.config.connection import DuckDBConnectionConfig, PostgresConnectionConfig
from sqlmesh.core.config.gateway import GatewayConfig

config = Config(
    # Gateway & Connection
    gateways={
        "prod": GatewayConfig(
            connection=PostgresConnectionConfig(
                host="localhost",
                port=5432,
                user="sqlmesh",
                password="secret",
                database="analytics",
            ),
        ),
        "dev": GatewayConfig(
            connection=DuckDBConnectionConfig(),
        ),
    },
    default_gateway="prod",

    # Project
    project="my_project",
    default_target_environment="prod",
    environment_ttl="7 days",

    # Model defaults
    model_defaults=ModelDefaultsConfig(dialect="postgres"),
    time_column_format="%Y-%m-%d",

    # Variables available in models/macros
    variables={"env": "production"},

    # Plan settings
    plan=PlanConfig(auto_categorize_changes=...),
)
```

### Key Config Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `gateways` | `Dict[str, GatewayConfig]` | `{"": GatewayConfig()}` | Gateway definitions |
| `default_gateway` | `str` | `""` | Default gateway name |
| `project` | `str` | `""` | Project name |
| `model_defaults` | `ModelDefaultsConfig` | `ModelDefaultsConfig()` | Default model settings |
| `variables` | `Dict[str, Any]` | `{}` | Variables for models/macros |
| `environment_ttl` | `Optional[str]` | Default TTL | Dev environment TTL |
| `pinned_environments` | `Set[str]` | `set()` | Environments not auto-cleaned |
| `default_target_environment` | `str` | `"prod"` | Default plan/run target |
| `physical_schema_mapping` | `Dict[Pattern, str]` | `{}` | Regex → physical schema mapping |
| `environment_suffix_target` | `EnvironmentSuffixTarget` | default | Suffix to schema or table |
| `ignore_patterns` | `List[str]` | defaults | Glob patterns to ignore |
| `plan` | `PlanConfig` | `PlanConfig()` | Plan settings |
| `run` | `RunConfig` | `RunConfig()` | Run settings |
| `notification_targets` | `List[NotificationTarget]` | `[]` | Notification targets |

### GatewayConfig

```python
GatewayConfig(
    connection: Optional[ConnectionConfig] = None,       # Main connection
    test_connection: Optional[ConnectionConfig] = None,  # Test connection
    state_connection: Optional[ConnectionConfig] = None,  # State DB connection
    scheduler: Optional[SchedulerConfig] = None,         # Scheduler config
)
```

### Supported Connection Types

| Connection Config Class | Engine |
|------------------------|--------|
| `DuckDBConnectionConfig` | DuckDB |
| `PostgresConnectionConfig` | PostgreSQL |
| `BigQueryConnectionConfig` | Google BigQuery |
| `SnowflakeConnectionConfig` | Snowflake |
| `DatabricksConnectionConfig` | Databricks |
| `RedshiftConnectionConfig` | AWS Redshift |
| `SparkConnectionConfig` | Apache Spark |
| `TrinoConnectionConfig` | Trino |
| `MSSQLConnectionConfig` | MS SQL Server |
| `MySQLConnectionConfig` | MySQL |
| `AthenaConnectionConfig` | AWS Athena |
| `MotherDuckConnectionConfig` | MotherDuck |
| `FabricConnectionConfig` | Microsoft Fabric |
| `GCPPostgresConnectionConfig` | GCP Cloud SQL Postgres |

### Load Config from YAML/Paths

```python
from sqlmesh.core.config import load_config_from_paths, load_config_from_yaml

# Load config from project paths
config = load_config_from_paths(Config, paths=["/path/to/project"])

# Load from YAML content
config = load_config_from_yaml(Config, yaml_content="...")
```

---

## 6. EngineAdapter — Database Operations

`EngineAdapter` abstracts database interactions across engines.

### Obtaining an Adapter

```python
# Via Context (recommended)
adapter = context.engine_adapter

# Create standalone
from sqlmesh.core.engine_adapter import create_engine_adapter

adapter = create_engine_adapter(
    connection_factory=lambda: connection,
    dialect="duckdb",
)
```

### Table Management

```python
# Create table
adapter.create_table(
    table_name="db.my_table",
    target_columns_to_types={"id": exp.DataType.build("INT"), "name": exp.DataType.build("VARCHAR")},
    primary_key=None,
    exists=True,
    table_description=None,
    column_descriptions=None,
)

# CTAS (Create Table As Select)
adapter.ctas(
    table_name="db.new_table",
    query_or_df="SELECT * FROM source",
    target_columns_to_types=None,
    exists=True,
)

# Drop table
adapter.drop_table(table_name="db.old_table", exists=True)

# Check existence
exists = adapter.table_exists(table_name="db.my_table")  # -> bool

# Get columns
cols = adapter.columns(table_name="db.my_table")  # -> Dict[str, exp.DataType]
```

### Data Manipulation

```python
# Replace entire table data
adapter.replace_query(
    table_name="db.my_table",
    query_or_df="SELECT * FROM staging",
    target_columns_to_types=None,
)

# Append data
adapter.insert_append(
    table_name="db.my_table",
    query_or_df="SELECT * FROM new_data",
    target_columns_to_types=None,
)

# Insert overwrite by time partition
adapter.insert_overwrite_by_time_partition(
    table_name="db.my_table",
    query_or_df="SELECT * FROM source",
    start="2024-01-01",
    end="2024-01-02",
    time_formatter="%Y-%m-%d",
    time_column="ds",
)

# Delete data
adapter.delete_from(table_name="db.my_table", where=exp.condition("ds < '2024-01-01'"))

# Update data
adapter.update_table(
    table_name="db.my_table",
    properties={"status": "'active'"},
    where="id > 100",
)
```

### View Management

```python
# Create view
adapter.create_view(
    view_name="db.my_view",
    query_or_df="SELECT * FROM db.my_table",
    replace=True,
    materialized=False,
)

# Drop view
adapter.drop_view(view_name="db.my_view", ignore_if_not_exists=True)
```

### Schema & Catalog Management

```python
adapter.create_schema(schema_name="db.new_schema", ignore_if_not_exists=True)
adapter.drop_schema(schema_name="db.old_schema", ignore_if_not_exists=True, cascade=False)
adapter.create_catalog(catalog_name="new_catalog")
adapter.drop_catalog(catalog_name="old_catalog")
adapter.get_current_catalog()  # -> Optional[str]
adapter.set_current_catalog("new_catalog")
```

### Table Alteration

```python
# Get required alter operations
operations = adapter.get_alter_operations(
    current_table_name="db.my_table",
    target_table_name="db.my_table_new",
    ignore_destructive=False,
    ignore_additive=False,
)  # -> List[TableAlterOperation]

# Execute alter operations
adapter.alter_table(alter_expressions=operations)
```

### Connection Lifecycle

```python
# Create adapter copy with different settings
new_adapter = adapter.with_settings(execute_log_level=logging.INFO)

# Recycle connections (close all except current thread)
adapter.recycle()

# Close all connections
adapter.close()
```

### Engine Capabilities (Class Attributes)

| Attribute | Default | Description |
|-----------|---------|-------------|
| `SUPPORTS_TRANSACTIONS` | `True` | Supports transactions |
| `SUPPORTS_INDEXES` | `False` | Supports indexes |
| `SUPPORTS_MATERIALIZED_VIEWS` | `False` | Supports materialized views |
| `SUPPORTS_CLONING` | `False` | Supports table cloning |
| `SUPPORTS_MANAGED_MODELS` | `False` | Supports managed models |
| `SUPPORTS_REPLACE_TABLE` | `True` | Supports CREATE OR REPLACE TABLE |
| `SUPPORTS_GRANTS` | `False` | Supports grant statements |
| `INSERT_OVERWRITE_STRATEGY` | `DELETE_INSERT` | Insert overwrite strategy |
| `DEFAULT_BATCH_SIZE` | `10000` | Default batch size |

### Supported Engine Adapters

| Adapter Class | Dialect Key |
|--------------|-------------|
| `DuckDBEngineAdapter` | `duckdb` |
| `PostgresEngineAdapter` | `postgres` |
| `BigQueryEngineAdapter` | `bigquery` |
| `SnowflakeEngineAdapter` | `snowflake` |
| `DatabricksEngineAdapter` | `databricks` |
| `RedshiftEngineAdapter` | `redshift` |
| `SparkEngineAdapter` | `spark`, `hive` |
| `TrinoEngineAdapter` | `trino` |
| `MSSQLEngineAdapter` | `mssql` |
| `MySQLEngineAdapter` | `mysql` |
| `AthenaEngineAdapter` | `athena` |
| `FabricEngineAdapter` | `fabric` |
| `RisingwaveEngineAdapter` | `risingwave` |
| `ClickhouseEngineAdapter` | `clickhouse` |

---

## 7. Macros

### `macro` Decorator

```python
from sqlmesh.core.macros import macro, MacroEvaluator, SQL

@macro()
def add_one(evaluator: MacroEvaluator, column: exp.Literal) -> exp.Add:
    """Adds 1 to a column value."""
    return evaluator.parse_one(f"{column} + 1")

# With metadata_only flag (not executed at runtime)
@macro(metadata_only=True)
def get_schema(evaluator: MacroEvaluator) -> exp.Literal:
    return exp.Literal.string(evaluator.var("schema_name"))
```

### `MacroEvaluator` — Core Evaluation Context

```python
class MacroEvaluator:
    # Key methods
    def send(self, name: str, *args, **kwargs)         # Call a registered macro
    def transform(self, expression: exp.Expression)      # Transform all macros in AST
    def evaluate(self, node: MacroFunc)                  # Evaluate a single macro node
    def parse_one(self, sql: str, **opts) -> exp.Expression  # Parse SQL
    def template(self, text, local_variables) -> str     # Template variable substitution
    def columns_to_types(self, model_name) -> Dict       # Get model column types
    def resolve_table(self, table: str) -> str           # Resolve model → physical table
    def resolve_tables(self, query: exp.Expression)      # Resolve all model refs in query
    def var(self, var_name: str, default=None)           # Get config variable
    def blueprint_var(self, var_name: str, default=None) # Get blueprint variable

    # Key properties
    @property runtime_stage -> RuntimeStage     # Current stage
    @property this_model -> str                 # Current model name
    @property engine_adapter -> EngineAdapter   # Engine adapter (not in LOADING)
    @property gateway -> Optional[str]          # Gateway name
    @property snapshots -> Dict[str, Snapshot]  # Available snapshots
```

### `RuntimeStage` Enum

```python
class RuntimeStage(Enum):
    LOADING = "loading"
    CREATING = "creating"
    EVALUATING = "evaluating"
    PROMOTING = "promoting"
    DEMOTING = "demoting"
    AUDITING = "auditing"
    TESTING = "testing"
    BEFORE_ALL = "before_all"
    AFTER_ALL = "after_all"
```

### Built-in Macros

| Macro | Description |
|-------|-------------|
| `@EACH(list, func)` | Iterate over list, apply function |
| `@IF(condition, then, else)` | Conditional evaluation |
| `@REDUCE(list, func)` | Reduce list to single value |
| `@FILTER(list, func)` | Filter list by function |
| `@WITH(expr)` | Conditionally add WITH clause |
| `@JOIN(expr)` | Conditionally add JOIN clause |
| `@WHERE(expr)` | Conditionally add WHERE clause |
| `@GROUP_BY(expr)` | Conditionally add GROUP BY clause |
| `@HAVING(expr)` | Conditionally add HAVING clause |
| `@ORDER_BY(expr)` | Conditionally add ORDER BY clause |
| `@LIMIT(expr)` | Conditionally add LIMIT clause |
| `@EVAL(expr)` | Evaluate Python/SQL expression |
| `@STAR(ref, exclude=, prefix=, suffix=)` | Column projection list |
| `@GENERATE_SURROGATE_KEY(*cols)` | MD5/SHA256 surrogate key |
| `@SAFE_ADD(*exprs)` | Safe addition (NULL handling) |
| `@SAFE_SUB(*exprs)` | Safe subtraction |
| `@SAFE_DIV(num, denom)` | Safe division (0 → NULL) |
| `@UNION(*tables)` | Union tables (matching columns) |
| `@HAVERSINE_DISTANCE(lat1, lon1, lat2, lon2)` | Haversine distance |
| `@PIVOT(column, then_value, else_value)` | Pivot projection |
| `@AND(*exprs)` | AND with NULL filtering |
| `@OR(*exprs)` | OR with NULL filtering |
| `@VAR(name)` | Get config variable |
| `@BLUEPRINT_VAR(name)` | Get blueprint variable |
| `@DEDUPLICATE(table, key, order)` | Deduplicate via ROW_NUMBER |
| `@DATE_SPINE(start, end, interval)` | Generate date spine |

---

## 8. Snapshots

### Key Classes

| Class | Description |
|-------|-------------|
| `Snapshot` | Versioned snapshot of a model |
| `SnapshotId` | Unique snapshot identifier |
| `SnapshotFingerprint` | Fingerprint for change detection |
| `SnapshotChangeCategory` | Change category (breaking/non-breaking/etc.) |
| `SnapshotDataVersion` | Data version info |
| `DeployabilityIndex` | Determines which snapshots are deployable |

### Snapshot Usage

```python
# Get snapshots from context
snapshots = context.snapshots  # Dict[str, Snapshot]

# Get specific snapshot
snapshot = context.get_snapshot("db.my_model", raise_if_missing=True)

# Snapshot key properties
snapshot.name           # Model name
snapshot.model          # Underlying Model object
snapshot.fingerprint    # SnapshotFingerprint
snapshot.version        # Snapshot version string
snapshot.change_category  # SnapshotChangeCategory
snapshot.intervals      # Materialized intervals
snapshot.is_model       # Whether this is a model snapshot
snapshot.is_audit       # Whether this is an audit snapshot
snapshot.categorized    # Whether change is categorized

# Physical table name
table_name = snapshot.table_name(is_deployable=True)
```

### Key Utility Functions

```python
from sqlmesh.core.snapshot import (
    missing_intervals,    # Calculate missing time intervals
    merge_intervals,     # Merge overlapping intervals
    snapshots_to_dag,    # Convert snapshots → DAG
    to_table_mapping,    # Generate table name mapping
)
```

---

## 9. Plan & PlanBuilder

### Plan Class

```python
from sqlmesh.core.plan import Plan, PlanBuilder, PlanStatus

# Plan represents the changes to apply
plan = context.plan("staging", auto_apply=False)

# Plan properties
plan.context_diff      # ContextDiff (added, modified, removed models)
plan.environment       # Target environment name
plan.snapshots         # Affected snapshots
plan.requires_backfill # Whether backfill is needed
plan.has_changes       # Whether any changes exist
```

### PlanBuilder

```python
# For fine-grained plan control
builder = context.plan_builder(
    environment="staging",
    skip_tests=True,
    forward_only=True,
    select_models=["db.my_model*"],
)

# Build the plan
plan = builder.build()

# Apply the plan
context.apply(plan)
```

### PlanStatus

| Status | Description |
|--------|-------------|
| `PlanStatus.CREATED` | Plan created but not yet applied |
| `PlanStatus.FAILED` | Plan execution failed |
| `PlanStatus.SUCCESS` | Plan successfully applied |

---

## 10. Logging & Utilities

### Logging Configuration

```python
from sqlmesh import configure_logging, enable_debug_mode

# Configure logging
configure_logging(
    force_debug: bool = False,          # Force debug mode
    write_to_stdout: bool = False,       # Also log to stdout
    write_to_file: bool = True,          # Write to log file
    log_file_dir: Optional[Union[str, Path]] = None,  # Custom log directory
    ignore_warnings: bool = False,       # Suppress WARNING in stdout
    log_level: Optional[Union[str, int]] = None,      # Custom level ("DEBUG", 10, etc.)
)

# Enable debug mode
enable_debug_mode()
```

### Runtime Environment Detection

```python
from sqlmesh import RuntimeEnv

env = RuntimeEnv.get()
env.is_terminal       # Running in terminal
env.is_databricks     # Running in Databricks
env.is_jupyter        # Running in Jupyter
env.is_google_colab   # Running in Colab
env.is_ci             # Running in CI/CD
env.is_notebook       # Running in any notebook
```

### CI/CD Detection

```python
from sqlmesh import is_cicd_environment, is_interactive_environment

is_cicd_environment()         # Checks CI, GITHUB_ACTIONS, TRAVIS, etc.
is_interactive_environment()  # Checks if stdin/stdout are TTY
```

### Date Utilities

```python
from sqlmesh.utils.date import (
    TimeLike,            # Union type for time-like values
    to_timestamp,        # Convert to timestamp
    to_datetime,         # Convert to datetime
    now_timestamp,       # Current UTC timestamp
    now,                 # Current UTC datetime
    make_exclusive,      # Convert inclusive bounds to exclusive
    format_tz_datetime,  # Format datetime with timezone
)
from sqlmesh import DatetimeRanges
```

---

## 11. Common SDK Patterns

### Pattern 1: Full CI/CD Pipeline

```python
from sqlmesh.core.context import Context

def deploy_to_prod(project_path: str):
    """Full deployment pipeline using SDK."""
    context = Context(paths=project_path, gateway="prod")

    # Create plan with auto-categorization
    plan = context.plan(
        environment="prod",
        auto_apply=False,
        no_prompts=True,
    )

    # Review plan before applying
    if plan.has_changes:
        print(f"Changes detected: {len(plan.context_diff.modified_snapshots)} modified")
        context.apply(plan)
        print("Plan applied successfully")
    else:
        print("No changes detected")
```

### Pattern 2: Model Evaluation & Testing

```python
from sqlmesh.core.context import Context
from datetime import datetime

def test_model_output(project_path: str, model_name: str):
    """Evaluate a model without side effects."""
    context = Context(paths=project_path)

    # Render the SQL query
    query = context.render(model_name, start="2024-01-01", end="2024-01-02")
    print(f"Rendered SQL:\n{query.sql()}")

    # Evaluate and get DataFrame
    df = context.evaluate(
        model_name,
        start="2024-01-01",
        end="2024-01-02",
        execution_time=datetime.utcnow(),
        limit=100,
    )
    print(f"Output shape: {df.shape}")
    print(df.head())

    # Run model tests
    context.test()
```

### Pattern 3: Programmatic Model Creation

```python
from sqlmesh.core.context import Context
from sqlmesh.core.model import create_sql_model, IncrementalByTimeRangeKind

def create_and_register_model(context: Context):
    """Create a model programmatically and register it."""
    model = create_sql_model(
        name="analytics.daily_revenue",
        query="""
            SELECT
                DATE(order_time) AS order_date,
                SUM(amount) AS total_revenue,
                COUNT(*) AS order_count
            FROM raw.orders
            GROUP BY DATE(order_time)
        """,
        kind=IncrementalByTimeRangeKind(time_column="order_date"),
        cron="@daily",
        start="2024-01-01",
        partitioned_by=["order_date"],
        tags=["revenue", "daily"],
    )

    # Upsert the model into the context
    context.upsert_model(model)
    print(f"Model '{model.name}' registered")
```

### Pattern 4: Data Quality Auditing

```python
from sqlmesh.core.context import Context

def run_audits(project_path: str):
    """Run audits on data quality."""
    context = Context(paths=project_path)

    # Run all audits for the last 7 days
    context.audit(start="7 days ago", end="now")

    # Evaluate a specific model and check
    df = context.evaluate("analytics.daily_revenue", start="2024-01-01", end="2024-01-02", execution_time="now")
    assert df["total_revenue"].notnull().all(), "Revenue should not be null"
```

### Pattern 5: Multi-Gateway Deployment

```python
from sqlmesh.core.context import Context
from sqlmesh.core.config import Config
from sqlmesh.core.config.connection import PostgresConnectionConfig, DuckDBConnectionConfig
from sqlmesh.core.config.gateway import GatewayConfig

def setup_multi_gateway():
    """Configure and use multiple gateways."""
    config = Config(
        gateways={
            "prod": GatewayConfig(
                connection=PostgresConnectionConfig(host="prod-db", database="analytics"),
            ),
            "dev": GatewayConfig(
                connection=DuckDBConnectionConfig(),
            ),
        },
        default_gateway="dev",
    )

    # Use dev gateway for development
    dev_ctx = Context(paths="./project", config=config, gateway="dev")
    dev_plan = dev_ctx.plan("dev_environment", auto_apply=True)

    # Use prod gateway for production
    prod_ctx = Context(paths="./project", config=config, gateway="prod")
    prod_plan = prod_ctx.plan("prod", auto_apply=False)
```

### Pattern 6: Scheduler Integration

```python
from sqlmesh.core.context import Context

def run_scheduler(project_path: str):
    """Use the built-in scheduler programmatically."""
    context = Context(paths=project_path)

    # Get the scheduler for a specific environment
    scheduler = context.scheduler(environment="prod")

    # Run the full DAG
    status = context.run(environment="prod", ignore_cron=True)
    print(f"Run status: {'success' if status.is_success else 'failed'}")
```

### Pattern 7: State Management

```python
from sqlmesh.core.context import Context

def inspect_state(project_path: str):
    """Inspect and manage SQLMesh state."""
    context = Context(paths=project_path)

    # Access state reader
    state = context.state_reader

    # Get all environments
    environments = state.get_environments()
    for env in environments:
        print(f"Environment: {env.name}, plan_id: {env.plan_id}")

    # Get prod environment details
    prod = state.get_environment("prod")
    if prod:
        print(f"Prod snapshots: {len(prod.snapshots)}")

    # Run janitor to clean up
    context.run_janitor(ignore_ttl=False)
```

---

## Appendix: Module Index

| Module | Key Exports |
|--------|------------|
| `sqlmesh` | `Config`, `Context`, `Model`, `model`, `macro`, `Snapshot`, `EngineAdapter` |
| `sqlmesh.core.context` | `Context`, `ExecutionContext` |
| `sqlmesh.core.model` | `Model`, `SqlModel`, `PythonModel`, `SeedModel`, `ExternalModel`, `model` |
| `sqlmesh.core.model.kind` | `FullKind`, `IncrementalByTimeRangeKind`, `ViewKind`, `CustomKind`, ... |
| `sqlmesh.core.config` | `Config`, `GatewayConfig`, `load_config_from_paths` |
| `sqlmesh.core.config.connection` | `DuckDBConnectionConfig`, `PostgresConnectionConfig`, ... |
| `sqlmesh.core.engine_adapter` | `EngineAdapter`, `create_engine_adapter` |
| `sqlmesh.core.macros` | `macro`, `MacroEvaluator`, `SQL` |
| `sqlmesh.core.snapshot` | `Snapshot`, `DeployabilityIndex`, `missing_intervals` |
| `sqlmesh.core.plan` | `Plan`, `PlanBuilder`, `PlanStatus` |
| `sqlmesh.utils.date` | `TimeLike`, `to_timestamp`, `now`, `DatetimeRanges` |
