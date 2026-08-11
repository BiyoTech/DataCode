# SQLMesh Documentation Index Tool — Design

> 为 Model Agent / Design Agent 提供 SQLMesh 文档的快速检索能力
> 解决 system prompt 中只能硬编码少量知识点、遇到复杂场景（语法细节/引擎配置/宏系统等）无法查阅的问题

---

## 1. 问题背景

### 1.1 现状

- `model_agent.yaml` 的 system prompt 中硬编码了少量 SQLMesh 知识（4 种 model kind、命名规范等）
- 69 个 MD 文档（~950KB）存放在 `sqlmesh_docs/` 目录下，涵盖概念/指南/集成/参考/FAQ 五大类
- Model Agent 在遇到复杂场景时（如 `INCREMENTAL_BY_TIME_RANGE` 的 `time_column` 配置细节、`@macro` 语法、特定引擎的 `kind` 参数）**没有工具可以查阅文档**

### 1.2 目标

设计一个 `sqlmesh_docs` 工具，让 Agent 可以：
1. **按关键词搜索**文档中的相关段落
2. **按主题浏览**（如列出所有 Model Kind 类型、查询 Macro 语法）
3. **按路径精确查阅**某个文档片段
4. **返回精简摘要**而非全量文档，避免 context 浪费

---

## 2. 架构设计

### 2.1 整体流程

```
┌──────────────────────────────────────────────────────────────────┐
│                      Application Startup                         │
│                                                                  │
│  sqlmesh_docs/ (69 .md files)                                    │
│        │                                                         │
│        ▼                                                         │
│  DocIndexer.parse_all()                                          │
│        │                                                         │
│        ├── Parse headings (##, ###, ####) per file               │
│        ├── Extract section text + file_path + line offset        │
│        └── Build inverted keyword → sections mapping             │
│        │                                                         │
│        ▼                                                         │
│  In-Memory Index (singleton, ~200-400KB)                         │
│        │                                                         │
└────────│─────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Runtime: Tool Call                          │
│                                                                  │
│  Agent calls: sqlmesh_docs(query="INCREMENTAL_BY_TIME_RANGE      │
│                              time_column format")                │
│        │                                                         │
│        ▼                                                         │
│  Keyword matching (tokenize query → lookup inverted index)       │
│        │                                                         │
│        ▼                                                         │
│  Rank results (keyword hit count × section priority)             │
│        │                                                         │
│        ▼                                                         │
│  Format output (title, file_path, excerpt, relevance score)      │
│        │                                                         │
│        ▼                                                         │
│  Return to Agent (max 3000 chars, top 5 results)                 │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/tools/sqlmesh_docs.py` | 新建 | Tool 定义 + DocIndexer |
| `src/subagents/model_agent.yaml` | 修改 | tools 列表增加 `sqlmesh_docs` |
| `src/subagents/design_agent.yaml` | 修改 | tools 列表增加 `sqlmesh_docs` |

> 索引构建在 ToolRegistry 初始化时自动完成，无需额外配置

---

## 3. 索引结构

### 3.1 数据模型

```python
@dataclass
class DocSection:
    """A document section identified by its Markdown heading."""
    title: str            # Section heading text (e.g., "INCREMENTAL_BY_TIME_RANGE")
    level: int            # Heading level (2=##, 3=###, 4=####)
    file_path: str        # Relative path (e.g., "concepts/models/model_kinds.md")
    category: str         # Top-level category (concepts / guides / integrations / reference / faq)
    content: str          # Section text body (trimmed to 500 chars)
    keywords: List[str]   # Extracted keywords from title + content

class DocIndex:
    """In-memory searchable index of all SQLMesh docs."""
    sections: List[DocSection]
    keyword_index: Dict[str, List[int]]  # keyword → list of section indices
    topic_tree: Dict[str, List[str]]     # category → list of file names
```

### 3.2 关键词提取策略

- **从标题提取**：分词 + 技术术语识别（大写+下划线模式如 `INCREMENTAL_BY_TIME_RANGE`）
- **从内容提取**：前 200 字符中的高频技术词（代码块中的 `MODEL (`, `kind`, `cron` 等）
- **停用词过滤**：英文通用词（the, a, is, of, and...）

### 3.3 索引内存估算

- 69 文件，平均每文件 ~8 个 section（heading），共 ~550 个 section
- 每个 section 取 500 字符内容 + 元数据 ≈ 700 字节
- 倒排索引 ≈ 200KB
- **总计 ~400-600KB**，内存开销可忽略

---

## 4. Tool API 设计

### 4.1 注册信息

```python
@ToolRegistry.register(
    "sqlmesh_docs",
    agents=["model_agent", "design_agent", "triage"],
    level=1,  # Read-only
    description="Search SQLMesh documentation for syntax, configuration, macros, engine adapters, and best practices",
)
```

### 4.2 函数签名

```python
@function_tool
async def sqlmesh_docs(
    ctx: RunContextWrapper[ToolContext],
    query: str = "",
    topic: str = "",
    file_path: str = "",
    list_topics: bool = False,
    limit: int = 5,
) -> str:
```

### 4.3 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | str | 否 | 自然语言或关键词搜索。例如 `"model kind full refresh"`, `"macro star"`, `"duckdb connection string"` |
| `topic` | str | 否 | 按主题浏览。可选值：`concepts`, `guides`, `integrations`, `reference`, `faq`, `engines`, `macros`, `models`, `configuration` |
| `file_path` | str | 否 | 精确查阅某文档。例如 `"concepts/models/model_kinds.md"` — 返回该文件的所有 section 摘要 |
| `list_topics` | bool | 否 | 设为 `true` 时列出所有可浏览的 topic 和其中包含的文档文件列表 |
| `limit` | int | 否 | 返回结果数上限（默认 5，最大 10） |

### 4.4 查询模式与使用场景

| 模式 | 参数组合 | 使用场景 |
|------|----------|----------|
| **关键词搜索** | `query="INCREMENTAL_BY_TIME_RANGE time_column"` | 用户问"增量模型怎么配置时间列" |
| **主题浏览** | `topic="model_kinds"` | Agent 需要了解有哪些 MODEL kind 可用 |
| **精确查阅** | `file_path="concepts/models/model_kinds.md"` | 用户想看 model kinds 的完整分类 |
| **文档导航** | `list_topics=true` | 用户问"有哪些文档可以看"，Agent 先列出目录 |

### 4.5 返回格式

```
## Search Results for: "INCREMENTAL_BY_TIME_RANGE time_column"

### 1. INCREMENTAL_BY_TIME_RANGE (Score: 12)
**File**: concepts/models/model_kinds.md
**Category**: concepts

Models of the INCREMENTAL_BY_TIME_RANGE kind are computed incrementally
based on a time range. This is an optimal choice for datasets where
records are captured over time and represent immutable facts...
[...500 chars excerpt]

### 2. Model configuration — kind (Score: 8)
**File**: reference/model_configuration.md
**Category**: reference

Configuration parameters for INCREMENTAL_BY_TIME_RANGE:
- time_column: The column containing timestamps...
[...500 chars excerpt]
```

---

## 5. Index Parser 实现细节

### 5.1 Markdown 解析策略

不使用重量级 Markdown 解析库，采用简单的行级解析：

```python
def _parse_file(file_path: str) -> List[DocSection]:
    """Parse a single MD file into sections based on heading structure."""
    sections = []
    current_heading = None
    current_level = 0
    current_lines = []

    for line in open(file_path):
        # Detect heading: ## / ### / ####
        if line.startswith("#") and not line.startswith("# "):  # skip h1
            # Save previous section
            if current_heading and current_lines:
                sections.append(_make_section(current_heading, current_level,
                                               current_lines, file_path))
            # Start new section
            level = line.count("#", 0, 6)  # count up to h6
            current_heading = line.lstrip("#").strip()
            current_level = level
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_heading and current_lines:
        sections.append(_make_section(current_heading, current_level,
                                       current_lines, file_path))
    return sections
```

### 5.2 关键词提取

```python
import re

TECH_TERM_PATTERN = re.compile(
    r'\b([A-Z][A-Z_]{2,}|[a-z]+_[a-z]+_[a-z]+|@\w+|\bkind\b|\bcron\b|\bgrain\b)'
)

def _extract_keywords(title: str, content: str) -> List[str]:
    words = set()
    # Title terms
    words.update(w.lower() for w in title.split())
    # Technical terms from content
    for match in TECH_TERM_PATTERN.finditer(content[:500]):
        words.add(match.group(1).lower())
    # Remove stopwords
    return [w for w in words if w not in STOPWORDS]
```

### 5.3 排序算法

```python
def _rank(query: str, sections: List[DocSection]) -> List[tuple]:
    """Score sections by keyword matches, weighted by heading level."""
    query_terms = set(query.lower().split())
    scored = []
    for i, s in enumerate(sections):
        title_hits = sum(1 for t in query_terms if t in s.title.lower())
        kw_hits = sum(1 for t in query_terms if t in s.keywords)
        # Higher heading level (h2) gets bonus
        level_bonus = 3 if s.level == 2 else 1
        score = title_hits * 5 + kw_hits * 2 + level_bonus
        if score > 0:
            scored.append((score, i))
    scored.sort(reverse=True)
    return scored
```

---

## 6. 与 Agent 的集成

### 6.1 注册到 ToolRegistry

```python
# src/tools/sqlmesh_docs.py

from src.tools.registry import ToolRegistry
from src.context.tool_context import ToolContext

@ToolRegistry.register(
    "sqlmesh_docs",
    agents=["model_agent", "design_agent", "triage"],
    level=1,
    description="Search SQLMesh documentation: syntax, model kinds, macros, "
                "engine configurations, CLI reference, and design patterns",
)
@function_tool
async def sqlmesh_docs(ctx: RunContextWrapper[ToolContext], ...) -> str:
    ...
```

### 6.2 修改 model_agent.yaml 的 tools 列表

在 `tools:` 列表末尾添加：

```yaml
tools:
  - schema_discovery
  - list_models
  - get_lineage
  - read_file
  - search_code
  - create_file
  - apply_patch
  - write_todo
  - preview_plan
  - apply_plan
  - sqlmesh_docs       # NEW
```

### 6.3 修改 model_agent.yaml 的 system_prompt

在 system_prompt 中增加使用指引（替代原有硬编码的 Model Kinds 表）：

```yaml
system_prompt: |
  ## SQLMesh Documentation:
  When you need precise SQLMesh syntax, model kind parameters, macro usage,
  engine-specific configuration, or CLI reference:
  - Use `sqlmesh_docs(query="...")` to search by keyword
  - Use `sqlmesh_docs(topic="model_kinds")` to browse available model kinds
  - Use `sqlmesh_docs(topic="macros")` to look up macro syntax
  - Use `sqlmesh_docs(file_path="...")` for a specific doc file
  - Use `sqlmesh_docs(list_topics=true)` to see what documentation is available
```

### 6.4 移除 system_prompt 中的硬编码内容

将 model_agent.yaml 第 41-45 行的：

```yaml
  ## Model Kinds:
  - **FULL**: Entire table is rebuilt each run.
  - **INCREMENTAL_BY_TIME_RANGE**: Only new time partitions...
  - **VIEW**: Lightweight SQL view...
  - **SEED**: Static data loaded from CSV files.
```

替换为一行指引：

```yaml
  ## Model Kinds:
  Use `sqlmesh_docs(topic="model_kinds")` to see all available model kinds and their configuration.
```

---

## 7. Topic 目录设计

### 7.1 预定义 Topic 与文件映射

| topic 值 | 包含文件 | 说明 |
|----------|----------|------|
| `concepts` | concepts/ 下所有文件 | 核心概念 |
| `guides` | guides/ 下所有文件 | 操作指南 |
| `integrations` | integrations/ 下所有文件（含 engines/） | 第三方集成 |
| `reference` | reference/ 下所有文件 | CLI/配置参考 |
| `faq` | faq/faq.md | 常见问题 |
| `engines` | integrations/engines/ 下所有文件 | 数据库引擎适配 |
| `macros` | concepts/macros/ 下所有文件 | 宏系统 |
| `models` | concepts/models/ 下所有文件 | 模型相关 |
| `model_kinds` | concepts/models/model_kinds.md | 模型类型（高频 topic） |
| `configuration` | guides/configuration.md + reference/configuration.md | 配置 |
| `cli` | reference/cli.md | CLI 命令参考 |
| `testing` | concepts/tests.md + guides/testing.md | 测试 |

### 7.2 `list_topics` 返回格式

```
## Available Documentation Topics

| Topic | Files | Description |
|-------|-------|-------------|
| concepts | 16 files | Core SQLMesh concepts |
| guides | 19 files | How-to guides and workflows |
| integrations | 21 files | Third-party integrations (dbt, engines) |
| reference | 6 files | CLI, configuration, API reference |
| faq | 1 file | Frequently asked questions |
| engines | 17 files | Database engine adapters |
| macros | 4 files | Jinja macros, SQLMesh macros |
| models | 7 files | Model definition and kinds |
| model_kinds | 1 file | Model kind reference |
| configuration | 2 files | Global + model configuration |
| cli | 1 file | CLI command reference |
```

---

## 8. 索引构建 & 刷新

### 8.1 构建时机

```python
class DocIndex:
    """Singleton — built once at module load time."""

    _instance: Optional["DocIndex"] = None

    @classmethod
    def get(cls) -> "DocIndex":
        if cls._instance is None:
            cls._instance = cls._build()
        return cls._instance

    @classmethod
    def _build(cls) -> "DocIndex":
        docs_dir = Path(__file__).parent.parent / "sqlmesh_docs"
        sections = []
        for md_file in sorted(docs_dir.rglob("*.md")):
            sections.extend(_parse_file(str(md_file), docs_dir))
        return cls(sections=sections)
```

模块首次被 import 时自动构建索引，后续调用直接使用缓存。

### 8.2 热刷新（可选）

```python
@classmethod
def refresh(cls) -> "DocIndex":
    """Rebuild index (e.g., after docs update)."""
    cls._instance = cls._build()
    return cls._instance
```

---

## 9. 错误处理与边界情况

| 场景 | 处理方式 |
|------|----------|
| 查询无匹配结果 | 返回 `No results found. Try sqlmesh_docs(list_topics=true) to see available topics.` |
| topic 不存在 | 返回 `Unknown topic '{topic}'. Available: concepts, guides, integrations, reference, faq, engines, macros, models, model_kinds, configuration, cli` |
| file_path 不存在 | 返回 `File '{file_path}' not found. Use list_topics=true to browse.` |
| 文档目录不存在 | 模块 import 时记录 warning，tool 调用时返回 `Documentation index not available.` |
| 超大查询词 | 截断 query 到 200 字符 |

---

## 10. 完整使用流程示例

### 场景 1：用户问"增量模型怎么配置时间列"

```
Agent: sqlmesh_docs(query="INCREMENTAL_BY_TIME_RANGE time_column config")
       ↓
Tool:  返回 model_kinds.md 中 INCREMENTAL_BY_TIME_RANGE 段（含 time_column 参数说明）
       和 reference/model_configuration.md 中 kind 配置表
       ↓
Agent: "INCREMENTAL_BY_TIME_RANGE 使用 time_column 参数指定时间列，
       语法：kind INCREMENTAL_BY_TIME_RANGE (time_column 'event_at')"
```

### 场景 2：用户问"Databricks 怎么连"

```
Agent: sqlmesh_docs(query="databricks connection configuration")
       ↓
Tool:  返回 integrations/engines/databricks.md 的相关 section
       ↓
Agent: "需要配置 gateway 的 connection 部分，type: databricks，
       server_hostname + http_path + token..."
```

### 场景 3：用户问"有哪些 model kind"

```
Agent: sqlmesh_docs(topic="model_kinds")
       ↓
Tool:  返回 model_kinds.md 的所有 heading 摘要
       列出：FULL, INCREMENTAL_BY_TIME_RANGE, VIEW, SEED, SCD_TYPE_2, EMBEDDED, EXTERNAL...
       ↓
Agent: 列出所有 kind 及简要说明
```

---

## 11. 总结

| 维度 | 设计决策 |
|------|----------|
| **索引结构** | 基于 Markdown heading 的行级解析，构建倒排索引 |
| **搜索方式** | 关键词匹配 + heading 层级加权，无需 embedding/向量库 |
| **内存占用** | ~400-600KB，构建在模块 import 时，无运行时开销 |
| **Tool 注册** | 挂载到 model_agent / design_agent / triage，level=1（只读） |
| **返回控制** | 每段截断 500 字符，默认 top 5，总量 < 3000 字符 |
| **扩展性** | 新增 MD 文件自动纳入索引，支持 topic 别名映射 |
| **产出文件** | `src/tools/sqlmesh_docs.py` (新增) + 2 个 YAML 修改 |
